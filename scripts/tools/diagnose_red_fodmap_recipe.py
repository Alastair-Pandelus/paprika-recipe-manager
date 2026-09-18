"""Diagnose a red FODMAP recipe and propose a non-red variant."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fodmap_score_lib import (  # noqa: E402
    TYPE_NAME,
    TYPE_ORDER,
    annotate_ingredients,
    emoji_for,
    meal_emoji,
    meal_score_emoji,
    pct,
    score_ingredient_line,
    strip_inline_fodmap,
)

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

# Prefer a familiar FD red recipe
CANDIDATES = [
    "Beef Bolognese",
    "Chicken Tagine",
    "Cottage Pie",
    "Chilli Con Carne",
]


def pick_recipe():
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    for needle in CANDIDATES:
        r = con.execute(
            """
            select uid, name, ingredients, description
            from recipes
            where coalesce(in_trash,0)=0
              and name like ?
              and name like '🔴%'
            order by name
            limit 1
            """,
            (f"%{needle}%",),
        ).fetchone()
        if r:
            con.close()
            return dict(r)
    # any FD red
    r = con.execute(
        """
        select uid, name, ingredients, description from recipes
        where coalesce(in_trash,0)=0 and name like '🔴 Field Doctor%'
        order by name limit 1
        """
    ).fetchone()
    con.close()
    return dict(r) if r else None


def diagnose(ingredients: str):
    rows = []
    stacks: dict[str, float] = {}
    for line in (ingredients or "").splitlines():
        if not line.strip():
            continue
        scored = score_ingredient_line(line)
        if not scored:
            continue
        clean, grams, green, ftype, no_lim = scored
        if no_lim or not ftype or not green:
            continue
        ratio = grams / green
        stacks[ftype] = stacks.get(ftype, 0.0) + ratio
        rows.append(
            {
                "line": clean,
                "grams": round(grams, 1),
                "green": green,
                "ftype": ftype,
                "ratio": ratio,
                "pct": pct(ratio),
                "emoji": emoji_for(ratio),
            }
        )
    return rows, stacks


def suggest_targets(stacks: dict[str, float]):
    """Scale factors per type to bring meal under red (≤150% = orange max)."""
    suggestions = []
    for t in TYPE_ORDER:
        load = stacks.get(t, 0.0)
        if load <= 1.5:
            continue
        # target 1.4 (still orange but under red) or 1.0 for yellow
        target = 1.4
        factor = target / load
        suggestions.append(
            {
                "type": t,
                "name": TYPE_NAME[t],
                "now_pct": pct(load),
                "target_pct": pct(target),
                "scale_drivers_to": round(factor, 2),
                "meal_emoji_if": meal_emoji(target),
            }
        )
    return suggestions


def main():
    rec = pick_recipe()
    if not rec:
        print("No red recipe found")
        return
    print("TEST RECIPE")
    print(rec["name"])
    print(f"UID {rec['uid']}")
    print()

    rows, stacks = diagnose(rec["ingredients"])
    print("DRIVERS (scored lines)")
    for r in sorted(rows, key=lambda x: -x["ratio"]):
        if r["emoji"] == "🟢" and r["ratio"] < 0.25:
            continue
        print(
            f"  {r['emoji']} {TYPE_NAME[r['ftype']]} {r['pct']}%  "
            f"(~{r['grams']:g}/{r['green']} g)  |  {r['line'][:70]}"
        )

    print()
    print("STACK TOTALS")
    for t in TYPE_ORDER:
        load = stacks.get(t, 0.0)
        if load < 0.05:
            continue
        print(f"  {meal_emoji(load)} {TYPE_NAME[t]} {pct(load)}%")

    print()
    print(f"MEAL NOW  {meal_score_emoji(stacks)}")
    print()
    print("TO GET NON-RED (meal ≤🟠, i.e. worst stack ≤150%)")
    sugg = suggest_targets(stacks)
    if not sugg:
        print("  Already ≤150% on all types?")
    for s in sugg:
        print(
            f"  {s['name']}: {s['now_pct']}% → ≤{s['target_pct']}%  "
            f"by scaling those ingredients ×{s['scale_drivers_to']} "
            f"(or drop/replace some)"
        )

    # Practical: show if we scale only the top contributors of the worst type
    worst_t = max(stacks, key=stacks.get) if stacks else None
    if worst_t and stacks[worst_t] > 1.5:
        contrib = [r for r in rows if r["ftype"] == worst_t]
        contrib.sort(key=lambda x: -x["ratio"])
        print()
        print(f"PRACTICAL LEVERS ({TYPE_NAME[worst_t]})")
        factor = 1.4 / stacks[worst_t]
        for r in contrib[:6]:
            new_g = round(r["grams"] * factor)
            print(
                f"  {r['line'][:55]}"
                f"  → ~{new_g} g  (was ~{r['grams']:g} g, ×{factor:.2f})"
            )


if __name__ == "__main__":
    main()
