"""Build Todoist Inbox meal tasks from a Favourites/freezer week plan.

One task per meal slot (breakfast / lunch / evening). Freezer take-out at 09:30
with labels meal+parallel and priority p1. Meal tasks: label meal, priority p4.

  python scripts/tools/todoist_meals_from_plan.py --start 2026-10-10
  python scripts/tools/todoist_meals_from_plan.py --start 2026-10-10 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from weekly_favourites_meal_plan import (  # noqa: E402
    FD_BOL_UID,
    Recipe,
    build_week,
    load_favourites,
    strip_marker,
)

MEAL_LABEL = "meal"
PARALLEL_LABEL = "parallel"

EMOJI = {
    "breakfast": "🍳",
    "lunch": "🥗",
    "dinner": "🍽️",
    "pudding": "🍰",
    "freezer": "🧊",
}


def keep_chol_title(name: str) -> str:
    """Strip FODMAP traffic icons; keep ♡ / ❣ / 💔 sat-fat marker + short name."""
    s = re.sub(
        r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*",
        "",
        (name or "").strip(),
    )
    if not re.match(r"^[♡❣◐💔♥💗]", s):
        s = "♡ " + strip_marker(name)
    s = s.replace("♥", "♡").replace("💗", "❣")
    return s


def bit(emoji: str, recipe: Recipe) -> str:
    return f"{emoji} {keep_chol_title(recipe.name)}"


def freezer_short_name(full: str) -> str:
    """Compact name for 'Take X out of freezer' titles."""
    s = strip_marker(full)
    s = re.sub(r"\s*\(Low FODMAP[^)]*\)\s*", "", s, flags=re.I).strip()
    s = re.sub(r"^Field Doctor\s+", "", s, flags=re.I).strip()
    s = re.sub(r"^Creamy Carrot Hummus.*", "carrot hummus", s, flags=re.I).strip()
    s = re.sub(
        r"^Beef Bolognese.*sauce for baked potato.*",
        "bolognese sauce",
        s,
        flags=re.I,
    ).strip()
    s = re.sub(r"^Beef Bolognese\s*\+?\s*Penne.*", "bolognese", s, flags=re.I).strip()
    s = re.sub(r"^Potato Topped Chicken Pie.*", "chicken pie", s, flags=re.I).strip()
    # Fallback: shorten long names
    if len(s) > 40:
        s = s[:37].rstrip() + "…"
    return s


def freezer_items_for_day(day: dict) -> list[tuple[str, str]]:
    """Freezer portions for the day as (short_name, slot) with slot lunch|dinner.

    Lunch items first, then dinner (chronological for a 09:30 take-out).
    """
    lunch_items: list[str] = []
    dinner_items: list[str] = []
    lunch = day.get("lunch")
    snack = day.get("lunch_snack")
    dinner = day.get("dinner")
    soup = day.get("soup")
    uses = day.get("pantry_uses") or {}

    if snack and getattr(snack, "from_freezer", False):
        lunch_items.append(strip_marker(snack.name))
    if uses.get(FD_BOL_UID) and lunch and "bolognese" in (lunch.name or "").lower():
        # Sauce for baked potato lunch — even if bol is also dinner that day,
        # lunch still needs a portion noted for lunch when potato is lunch.
        if dinner and getattr(dinner, "from_freezer", False) and "bolognese" in (
            dinner.name or ""
        ).lower():
            # Dinner bol covers the evening; lunch potato still needs sauce
            lunch_items.append("Field Doctor Beef Bolognese (sauce for baked potato)")
        else:
            lunch_items.append("Field Doctor Beef Bolognese (sauce for baked potato)")
    if soup and getattr(soup, "from_freezer", False):
        dinner_items.append(strip_marker(soup.name))
    if dinner and getattr(dinner, "from_freezer", False):
        dinner_items.append(strip_marker(dinner.name))

    def dedupe(names: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in names:
            if it in seen:
                continue
            seen.add(it)
            out.append(it)
        return out

    ordered: list[tuple[str, str]] = []
    for it in dedupe(lunch_items):
        ordered.append((freezer_short_name(it), "lunch"))
    for it in dedupe(dinner_items):
        ordered.append((freezer_short_name(it), "dinner"))
    return ordered


def tasks_for_day(day: dict) -> list[dict]:
    """Todoist add-tasks payloads for one plan day."""
    d: date = day["date"]
    day_str = d.strftime("%Y-%m-%d")
    due_day = d.isoformat()
    tasks: list[dict] = []

    b: Recipe = day["breakfast"]
    tasks.append(
        {
            "content": bit(EMOJI["breakfast"], b),
            "description": f"tasker-paprika:{b.uid}",
            "projectId": "inbox",
            "priority": "p4",
            "labels": [MEAL_LABEL],
            "dueString": f"on {due_day} at 9:00 am",
            "duration": "30m",
        }
    )

    frozen = freezer_items_for_day(day)
    if frozen:
        # e.g. "carrot hummus (lunch), chicken pie (dinner)"
        parts = [f"{name} ({slot})" for name, slot in frozen]
        take = ", ".join(parts)
        tasks.append(
            {
                "content": f"{EMOJI['freezer']} Take {take} out of freezer",
                "description": (
                    "Move to the fridge if it needs the day to defrost.\n"
                    f"tasker-paprika:freezer:{day_str}"
                ),
                "projectId": "inbox",
                "priority": "p1",
                "labels": [MEAL_LABEL, PARALLEL_LABEL],
                "dueString": f"on {due_day} at 9:30 am",
                "duration": "15m",
            }
        )

    lunch: Recipe = day["lunch"]
    snack = day.get("lunch_snack")
    lunch_title = bit(EMOJI["lunch"], lunch)
    if snack:
        lunch_title += " " + bit(EMOJI["lunch"], snack)
    tasks.append(
        {
            "content": lunch_title,
            "description": f"tasker-paprika:{lunch.uid}",
            "projectId": "inbox",
            "priority": "p4",
            "labels": [MEAL_LABEL],
            "dueString": f"on {due_day} at 1:00 pm",
            "duration": "45m",
        }
    )

    dinner: Recipe = day["dinner"]
    soup = day.get("soup")
    pudding: Recipe = day["pudding"]
    evening_parts = []
    if soup:
        evening_parts.append(bit(EMOJI["dinner"], soup))
    evening_parts.append(bit(EMOJI["dinner"], dinner))
    evening_parts.append(bit(EMOJI["pudding"], pudding))
    tasks.append(
        {
            "content": " ".join(evening_parts),
            "description": f"tasker-paprika:{dinner.uid}",
            "projectId": "inbox",
            "priority": "p4",
            "labels": [MEAL_LABEL],
            "dueString": f"on {due_day} at 8:00 pm",
            "duration": "45m",
        }
    )
    return tasks


def build_todoist_tasks(start: date) -> list[dict]:
    plan = build_week(start, load_favourites())
    out: list[dict] = []
    for day in plan:
        out.extend(tasks_for_day(day))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--json", action="store_true", help="Print JSON payloads only")
    args = ap.parse_args()
    start = (
        date.fromisoformat(args.start) if args.start else date.today() + timedelta(days=1)
    )
    tasks = build_todoist_tasks(start)
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return
    print(f"Todoist meal tasks for week of {start.isoformat()}: {len(tasks)}\n")
    for t in tasks:
        labs = " ".join(f"#{x}" for x in (t.get("labels") or []))
        print(
            f"- [{t['priority']}] {t['dueString']} ({t.get('duration')}) {labs}\n"
            f"  {t['content'][:110]}"
        )


if __name__ == "__main__":
    main()
