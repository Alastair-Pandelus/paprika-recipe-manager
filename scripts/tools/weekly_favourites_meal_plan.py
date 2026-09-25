"""Dry-run 7-day Favourites meal plan + shopping list.

Rules: Favourites only; B/L/D + pudding; rotate; French toast ~4 mornings/week;
no consecutive bread meals within the same day only; exclude fruit-salad desserts;
prefer home LF dinners over Waitrose packs.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
FAV_PARENT = "01A0427C-9963-4D6B-ACAF-5EA469E6ED1C"

# Shared aisle heuristics (same as apply_weekly_favourites_to_paprika)
try:
    from paprika_grocery_aisles import aisle_for  # type: ignore
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paprika_grocery_aisles import aisle_for  # noqa: E402

BREAD_RE = re.compile(
    r"\b(toast|sourbread|sourdough bread|french toast|french toasty|"
    r"bread pizza|baguette|breadcrumb|crumbed|breadcrumbs)\b",
    re.I,
)
SHOP_RE = re.compile(r"^waitrose\b", re.I)
FRUIT_SALAD_RE = re.compile(r"fruit\s*salad", re.I)


@dataclass
class Recipe:
    uid: str
    name: str
    slot: str
    ingredients: str
    is_bread: bool
    is_shop: bool
    prefer_breakfast: bool  # French toast


def strip_marker(name: str) -> str:
    return re.sub(r"^[✅ℹ️❌🟢🟡🟠🔴⚠️]\s*", "", name or "").strip()


def load_favourites() -> dict[str, list[Recipe]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    slots: dict[str, list[Recipe]] = {
        "Breakfast": [],
        "Lunch": [],
        "Dinner": [],
        "Dessert": [],
    }
    for row in con.execute(
        "SELECT uid, name FROM recipe_categories WHERE parent_uid=?",
        (FAV_PARENT,),
    ):
        slot = row["name"]
        if slot not in slots:
            continue
        for r in con.execute(
            """SELECT r.uid, r.name, r.ingredients
               FROM recipes r
               JOIN recipes_to_categories rtc ON rtc.recipe_uid=r.uid
               WHERE rtc.category_uid=? AND coalesce(r.in_trash,0)=0""",
            (row["uid"],),
        ):
            name = r["name"] or ""
            ings = r["ingredients"] or ""
            blob = f"{name}\n{ings}"
            is_bread = bool(BREAD_RE.search(blob))
            is_shop = bool(SHOP_RE.search(strip_marker(name)))
            prefer = "french toast" in name.lower() and "toasty" not in name.lower()
            slots[slot].append(
                Recipe(
                    uid=r["uid"],
                    name=name,
                    slot=slot,
                    ingredients=ings,
                    is_bread=is_bread,
                    is_shop=is_shop,
                    prefer_breakfast=prefer,
                )
            )
    return slots


def dinner_pool(dinners: list[Recipe]) -> list[Recipe]:
    """Prefer Waitrose packs when a home LF twin exists; else home / unpaired dinners."""
    shops = [d for d in dinners if d.is_shop]
    homes = [d for d in dinners if not d.is_shop]

    def twin_key(name: str) -> str:
        n = strip_marker(name).lower()
        n = re.sub(r"^low fodmap\s+", "", n)
        n = re.sub(r"^waitrose\s+", "", n)
        # shared dish tokens
        for token in (
            "chicken pad thai",
            "chicken teriyaki donburi",
            "chicken nasi goreng",
            "bbq pork with sticky rice",
            "bbq pork",
            "pad thai",
            "teriyaki donburi",
            "nasi goreng",
        ):
            if token in n:
                return token
        return n

    shop_keys = {twin_key(s.name) for s in shops}
    # Drop home recipes that duplicate a Waitrose pair
    unpaired_home = [h for h in homes if twin_key(h.name) not in shop_keys]
    pool = shops + unpaired_home
    return pool if pool else dinners


def dessert_pool(desserts: list[Recipe]) -> list[Recipe]:
    """Plain fruit salad is out; fruit salad with ice cream is in."""
    eligible = []
    for d in desserts:
        name = d.name or ""
        if FRUIT_SALAD_RE.search(name) and not re.search(r"ice\s*cream", name, re.I):
            continue  # fruit salad alone
        eligible.append(d)
    return eligible if eligible else desserts


def pick(
    pool: list[Recipe],
    *,
    used_week: set[str],
    used_yesterday: set[str],
    need_not_bread: bool,
    exclude: set[str] | None = None,
) -> Recipe:
    candidates = list(pool)
    exclude = exclude or set()
    candidates = [c for c in candidates if c.uid not in exclude]
    if need_not_bread:
        nb = [c for c in candidates if not c.is_bread]
        if nb:
            candidates = nb
    if not candidates:
        candidates = list(pool)
        if need_not_bread:
            nb = [c for c in candidates if not c.is_bread]
            if nb:
                candidates = nb

    def score(c: Recipe) -> tuple:
        return (
            0 if c.uid not in used_yesterday else 1,
            0 if c.uid not in used_week else 1,
            strip_marker(c.name).lower(),
        )

    return sorted(candidates, key=score)[0]


def build_week(start: date, slots: dict[str, list[Recipe]]) -> list[dict]:
    breakfasts = slots["Breakfast"]
    lunches = slots["Lunch"]
    dinners = dinner_pool(slots["Dinner"])
    desserts = dessert_pool(slots["Dessert"])

    french = next((b for b in breakfasts if b.prefer_breakfast), None)
    other_bfast = [b for b in breakfasts if not b.prefer_breakfast]
    french_days = {0, 2, 4, 5}

    shop_dinners = sorted(
        [d for d in dinners if d.is_shop],
        key=lambda x: strip_marker(x.name).lower(),
    )
    home_dinners = [d for d in dinners if not d.is_shop]
    # Front-load Waitrose early in the week (best-before)
    shop_queue = list(shop_dinners)

    used_b: set[str] = set()
    used_l: set[str] = set()
    used_d: set[str] = set()
    used_p: set[str] = set()
    yday_b: set[str] = set()
    yday_l: set[str] = set()
    yday_d: set[str] = set()
    plan = []

    for i in range(7):
        day = start + timedelta(days=i)

        if french and i in french_days:
            b = french
        else:
            pool_b = other_bfast or breakfasts
            b = pick(
                pool_b,
                used_week=used_b,
                used_yesterday=yday_b,
                need_not_bread=False,
                exclude={french.uid} if french else set(),
            )
            if french and b.uid == french.uid and i not in french_days:
                b = pick(
                    other_bfast or breakfasts,
                    used_week=used_b,
                    used_yesterday=yday_b,
                    need_not_bread=False,
                    exclude={french.uid},
                )

        l = pick(
            lunches,
            used_week=used_l,
            used_yesterday=yday_l,
            need_not_bread=b.is_bread,
        )

        # Dinner: use next Waitrose pack while queue remains and bread-safe;
        # otherwise home / remaining pool
        d = None
        if shop_queue:
            for idx, cand in enumerate(shop_queue):
                if l.is_bread and cand.is_bread:
                    continue
                d = shop_queue.pop(idx)
                break
        if d is None:
            d = pick(
                home_dinners or dinners,
                used_week=used_d,
                used_yesterday=yday_d,
                need_not_bread=l.is_bread,
            )

        p = pick(
            desserts,
            used_week=used_p,
            used_yesterday=set(),
            need_not_bread=False,
        )

        used_b.add(b.uid)
        used_l.add(l.uid)
        used_d.add(d.uid)
        used_p.add(p.uid)
        yday_b, yday_l, yday_d = {b.uid}, {l.uid}, {d.uid}

        plan.append(
            {
                "date": day,
                "breakfast": b,
                "lunch": l,
                "dinner": d,
                "pudding": p,
            }
        )
    return plan


def shopping_list(plan: list[dict]) -> dict[str, list[str]]:
    """Naive line merge from ingredients (skip section headers)."""
    counts: dict[str, int] = defaultdict(int)
    for day in plan:
        for key in ("breakfast", "lunch", "dinner", "pudding"):
            r: Recipe = day[key]
            if r.is_shop:
                counts[f"[PACK] {strip_marker(r.name)}"] += 1
                continue
            for ln in (r.ingredients or "").splitlines():
                ln = ln.strip()
                if (
                    not ln
                    or ln.isupper()
                    or ln.startswith("OPTIONAL")
                    or ln.startswith("SAUCE")
                    or ln.startswith("TO ")
                ):
                    continue
                if ln.startswith(
                    (
                        "RICE",
                        "CHICKEN",
                        "PORK",
                        "GREENS",
                        "TERIYAKI",
                        "BBQ",
                        "STICKY",
                    )
                ):
                    continue
                ln = re.sub(r"\s*[🟢🟡🟠🔴]\s*.*$", "", ln).strip()
                if len(ln) < 3:
                    continue
                counts[ln] += 1

    aisles: dict[str, list[str]] = defaultdict(list)
    for item, n in sorted(counts.items(), key=lambda x: x[0].lower()):
        label = item if n == 1 else f"{item}  ×{n}"
        is_pack = item.lower().startswith("[pack]")
        aisle = aisle_for(item, is_pack=is_pack)
        aisles[aisle].append(label)
    return dict(aisles)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: tomorrow)")
    args = ap.parse_args()
    start = (
        date.fromisoformat(args.start) if args.start else date.today() + timedelta(days=1)
    )

    slots = load_favourites()
    plan = build_week(start, slots)

    print(f"## 7-day Favourites plan from {start.isoformat()}\n")
    print("| Day | Breakfast | Lunch | Dinner | Pudding |")
    print("|-----|-----------|-------|--------|---------|")
    for day in plan:
        d = day["date"].strftime("%a %d %b")
        cells = []
        for key in ("breakfast", "lunch", "dinner", "pudding"):
            r: Recipe = day[key]
            mark = ""
            if r.is_bread:
                mark = " 🍞"
            if r.is_shop:
                mark += " 🛒"
            cells.append(strip_marker(r.name)[:42] + mark)
        print(f"| {d} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")

    print("\n### Bread checks (same day only)")
    for day in plan:
        seq = [
            ("B", day["breakfast"]),
            ("L", day["lunch"]),
            ("D", day["dinner"]),
        ]
        for (a_lab, a), (b_lab, b) in zip(seq, seq[1:]):
            if a.is_bread and b.is_bread:
                print(
                    f"⚠ {day['date']}: consecutive bread {a_lab}→{b_lab}: "
                    f"{strip_marker(a.name)} / {strip_marker(b.name)}"
                )
    print("(no consecutive bread pairs listed above = OK)\n")

    ft_count = sum(1 for d in plan if d["breakfast"].prefer_breakfast)
    print(f"French toast breakfasts: {ft_count}/7")
    puds = {strip_marker(d["pudding"].name) for d in plan}
    print(f"Puddings used: {', '.join(sorted(puds))}\n")

    print("## Shopping list (merged)\n")
    for aisle, items in shopping_list(plan).items():
        print(f"### {aisle}")
        for it in items:
            print(f"- {it}")
        print()


if __name__ == "__main__":
    main()
