"""Build Todoist payloads from Paprika Meals already on the calendar (authoritative)."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from todoist_meals_from_plan import (  # noqa: E402
    EMOJI,
    MEAL_LABEL,
    PARALLEL_LABEL,
    bit,
    keep_chol_title,
)
from weekly_favourites_meal_plan import Recipe, recipe_from_row, strip_marker  # noqa: E402

LOCAL = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
HUMMUS_UID = "712FDA6F-02BB-4E09-8DB2-190759D0248A"
FD_BOL_UID = "E47DB200-625A-488F-AA9B-1E4A7EBB3178"
FD_PIE_UID = "9B4399CB-969B-423A-9C2D-86C8F8E63580"


def load_meals(start: date, days: int = 7) -> dict[str, list[dict]]:
    end = start + timedelta(days=days - 1)
    con = sqlite3.connect(LOCAL)
    con.row_factory = sqlite3.Row
    types = {
        r["uid"]: (r["name"] or "")
        for r in con.execute("SELECT uid, name FROM meal_types")
    }
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute(
        """
        SELECT m.date, m.name, m.recipe_uid, m.type_uid, m.order_flag,
               r.ingredients, r.name as rname
        FROM meals m
        LEFT JOIN recipes r ON r.uid = m.recipe_uid
        WHERE coalesce(m.status,'') != 'deleted'
          AND substr(m.date,1,10) >= ? AND substr(m.date,1,10) <= ?
        ORDER BY m.date, m.order_flag
        """,
        (start.isoformat(), end.isoformat()),
    ):
        day = (r["date"] or "")[:10]
        tname = types.get(r["type_uid"], "")
        by_day[day].append(
            {
                "type": tname,
                "name": r["rname"] or r["name"] or "",
                "recipe_uid": r["recipe_uid"],
                "ingredients": r["ingredients"] or "",
                "order": r["order_flag"] or 0,
            }
        )
    con.close()
    return by_day


def as_recipe(row: dict, slot: str) -> Recipe:
    return recipe_from_row(
        row["recipe_uid"] or "",
        row["name"],
        row["ingredients"],
        slot=slot,
    )


def is_snack(name: str) -> bool:
    n = name.lower()
    return "hummus" in n or "oatcakes" in n


def is_soup(name: str) -> bool:
    return "soup" in name.lower()


def freezer_names(day_rows: list[dict]) -> list[str]:
    """Heuristic: FD pie/bol/hummus on the day count as freezer take-out."""
    items = []
    for row in day_rows:
        uid = row["recipe_uid"] or ""
        name = strip_marker(row["name"])
        if uid == HUMMUS_UID or uid == FD_PIE_UID or uid == FD_BOL_UID:
            items.append(name)
        elif uid == FD_BOL_UID:
            items.append(name)
    # baked potato day without bol dinner still needs sauce if bol was pantry —
    # if any lunch is baked potato and bol appears same day as dinner OR we know
    # from prior plan: if baked potato and no FD bol dinner, still list sauce when
    # hummus/pie/bol uids present in pantry_uses — here detect baked potato + no
    # bol dinner → sauce
    has_potato = any("baked potato" in (r["name"] or "").lower() for r in day_rows)
    has_bol_dinner = any(
        r["recipe_uid"] == FD_BOL_UID
        and not is_snack(r["name"])
        and (r["type"] or "").lower() == "dinner"
        for r in day_rows
    )
    if has_potato and not has_bol_dinner:
        items.append("Field Doctor Beef Bolognese (sauce for baked potato)")
    # dedupe
    seen = set()
    out = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def tasks_from_calendar(start: date) -> list[dict]:
    by_day = load_meals(start)
    tasks: list[dict] = []
    for i in range(7):
        d = start + timedelta(days=i)
        day_str = d.isoformat()
        rows = by_day.get(day_str, [])
        if not rows:
            continue

        breakfasts = [r for r in rows if (r["type"] or "").lower() == "breakfast"]
        lunches = [r for r in rows if (r["type"] or "").lower() == "lunch"]
        dinners = [r for r in rows if (r["type"] or "").lower() == "dinner"]
        desserts = [
            r
            for r in rows
            if (r["type"] or "").lower() in ("dessert", "pudding")
        ]

        if breakfasts:
            b = as_recipe(breakfasts[0], "Breakfast")
            tasks.append(
                {
                    "content": bit(EMOJI["breakfast"], b),
                    "description": f"tasker-paprika:{b.uid}",
                    "projectId": "inbox",
                    "priority": "p4",
                    "labels": [MEAL_LABEL],
                    "dueString": f"on {day_str} at 9:00 am",
                    "duration": "30m",
                }
            )

        frozen = freezer_names(rows)
        if frozen:
            tasks.append(
                {
                    "content": f"{EMOJI['freezer']} Prep Lunch & Dinner",
                    "description": (
                        "Take anything out of the freezer for lunch and dinner. "
                        "Move it to the fridge if it needs the day to defrost.\n\n"
                        f"Today: {'; '.join(frozen)}"
                    ),
                    "projectId": "inbox",
                    "priority": "p1",
                    "labels": [MEAL_LABEL, PARALLEL_LABEL],
                    "dueString": f"on {day_str} at 9:30 am",
                    "duration": "15m",
                }
            )

        mains = [r for r in lunches if not is_snack(r["name"])]
        snacks = [r for r in lunches if is_snack(r["name"])]
        if mains or snacks:
            parts = []
            uid = ""
            if mains:
                lr = as_recipe(mains[0], "Lunch")
                uid = lr.uid
                parts.append(bit(EMOJI["lunch"], lr))
            for s in snacks:
                sr = as_recipe(s, "Lunch")
                if not uid:
                    uid = sr.uid
                parts.append(bit(EMOJI["lunch"], sr))
            tasks.append(
                {
                    "content": " ".join(parts),
                    "description": f"tasker-paprika:{uid}",
                    "projectId": "inbox",
                    "priority": "p4",
                    "labels": [MEAL_LABEL],
                    "dueString": f"on {day_str} at 1:00 pm",
                    "duration": "45m",
                }
            )

        soup_rows = [r for r in dinners if is_soup(r["name"])]
        dinner_mains = [r for r in dinners if not is_soup(r["name"])]
        evening = []
        dinner_uid = ""
        for s in soup_rows:
            sr = as_recipe(s, "Dinner")
            evening.append(bit(EMOJI["dinner"], sr))
        for m in dinner_mains:
            mr = as_recipe(m, "Dinner")
            dinner_uid = mr.uid
            evening.append(bit(EMOJI["dinner"], mr))
        for p in desserts:
            pr = as_recipe(p, "Dessert")
            evening.append(bit(EMOJI["pudding"], pr))
            if not dinner_uid:
                dinner_uid = pr.uid
        if evening:
            tasks.append(
                {
                    "content": " ".join(evening),
                    "description": f"tasker-paprika:{dinner_uid}",
                    "projectId": "inbox",
                    "priority": "p4",
                    "labels": [MEAL_LABEL],
                    "dueString": f"on {day_str} at 8:00 pm",
                    "duration": "45m",
                }
            )
    return tasks


if __name__ == "__main__":
    start = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-10")
    tasks = tasks_from_calendar(start)
    print(json.dumps(tasks, ensure_ascii=False, indent=2))
    print(f"\n# {len(tasks)} tasks", file=sys.stderr)
    for t in tasks:
        print(f"{t['dueString']}: {t['content'][:90]}", file=sys.stderr)
