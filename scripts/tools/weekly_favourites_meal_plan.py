"""Dry-run 7-day Favourites + freezer pantry meal plan + shopping list.

Rules: Favourites + pantry freezer + grocery-linked recipes; B/L/D + pudding;
rotate / maximise dinner diversity; French toast ~4 mornings/week;
no consecutive bread meals within the same day only; exclude fruit-salad-alone desserts;
prefer Waitrose packs when paired with a home LF twin; front-load packs early in the week;
draw soups/mains/spreads from pantry when stocked (mark 🧊); breakfast never frozen.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
FAV_PARENT = "01A0427C-9963-4D6B-ACAF-5EA469E6ED1C"

# Library recipes used from freezer pantry (may sit outside Favourites folders)
HUMMUS_UID = "712FDA6F-02BB-4E09-8DB2-190759D0248A"
FD_PIE_UID = "9B4399CB-969B-423A-9C2D-86C8F8E63580"
FD_BOL_UID = "E47DB200-625A-488F-AA9B-1E4A7EBB3178"
FD_FISH_PIE_UID = "B7A0C22C-577B-481B-AF69-28FAF466D2F1"
PARSNIP_SOUP_UID = "ABEFF3EC-1E5F-44F8-B8FE-5A42DB7AFA35"

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
    uses_half_grapefruit: bool = False  # cut fruit — pair serves ~2 days apart
    from_freezer: bool = False  # portion drawn from pantry freezer stock


def strip_marker(name: str) -> str:
    """Strip leading FODMAP + sat-fat title icons for matching."""
    s = name or ""
    s = re.sub(
        r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴|♡|❣|◐|💔|♥)\s*",
        "",
        s,
    )
    # May have FODMAP then chol (or reverse on legacy titles)
    s = re.sub(
        r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴|♡|❣|◐|💔|♥)\s*",
        "",
        s,
    )
    return s.strip()


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
            uses_gf = bool(re.search(r"\bgrapefruit\b", blob, re.I))
            slots[slot].append(
                Recipe(
                    uid=r["uid"],
                    name=name,
                    slot=slot,
                    ingredients=ings,
                    is_bread=is_bread,
                    is_shop=is_shop,
                    prefer_breakfast=prefer,
                    uses_half_grapefruit=uses_gf,
                )
            )
    con.close()
    return slots


def recipe_from_row(uid: str, name: str, ings: str, slot: str = "") -> Recipe:
    blob = f"{name}\n{ings}"
    return Recipe(
        uid=uid,
        name=name or "",
        slot=slot,
        ingredients=ings or "",
        is_bread=bool(BREAD_RE.search(blob)),
        is_shop=bool(SHOP_RE.search(strip_marker(name or ""))),
        prefer_breakfast=(
            "french toast" in (name or "").lower() and "toasty" not in (name or "").lower()
        ),
        uses_half_grapefruit=bool(re.search(r"\bgrapefruit\b", blob, re.I)),
        from_freezer=False,
    )


def load_recipe_uid(uid: str, *, slot: str = "") -> Recipe | None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT uid, name, ingredients FROM recipes "
        "WHERE uid=? AND coalesce(in_trash,0)=0",
        (uid,),
    ).fetchone()
    con.close()
    if not r:
        return None
    return recipe_from_row(r["uid"], r["name"] or "", r["ingredients"] or "", slot)


def load_pantry_stock() -> dict[str, int]:
    """Map recipe uid → portion qty from freezer pantry lines."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT quantity, ingredient FROM pantry_items WHERE coalesce(in_stock,0)=1"
    ).fetchall()
    recipes = {
        r["uid"]: strip_marker(r["name"] or "")
        for r in con.execute(
            "SELECT uid, name FROM recipes WHERE coalesce(in_trash,0)=0"
        )
    }
    con.close()
    stock: dict[str, int] = defaultdict(int)
    for row in rows:
        try:
            qty = int(str(row["quantity"] or "0").strip() or "0")
        except ValueError:
            qty = 0
        if qty <= 0:
            continue
        ing = strip_marker(row["ingredient"] or "")
        if not ing:
            continue
        ing_l = ing.lower()
        match_uid = None
        for uid, name in recipes.items():
            nl = name.lower()
            if nl == ing_l or nl in ing_l or ing_l in nl:
                match_uid = uid
                break
        if match_uid:
            stock[match_uid] += qty
    return dict(stock)


def as_freezer(r: Recipe) -> Recipe:
    return replace(r, from_freezer=True)


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


def grapefruit_pair_days(french_days: set[int]) -> tuple[int, int] | None:
    """Pick (first, second) breakfast indices for half-grapefruit: prefer +2 days."""
    non_french = [i for i in range(7) if i not in french_days]
    # Prefer first on a non-French morning with N+2 still in week
    for first in non_french:
        follow = first + 2
        if follow < 7:
            return first, follow
    # Else first on non-French with N+1 in week (consecutive — last resort)
    for first in non_french:
        follow = first + 1
        if follow < 7:
            return first, follow
    # Else earliest day that still allows +2 (may displace French toast)
    for first in range(5):  # 0..4 → follow 2..6
        return first, first + 2
    if 5 < 7:
        return 5, 6
    return None


def build_week(start: date, slots: dict[str, list[Recipe]]) -> list[dict]:
    """Build week; when freezer pantry has stock, use freezer-aware schedule."""
    stock = load_pantry_stock()
    if stock.get(FD_PIE_UID) or stock.get(FD_BOL_UID) or stock.get(HUMMUS_UID):
        return build_week_freezer(start, slots, stock)
    return build_week_favourites_only(start, slots)


def build_week_favourites_only(start: date, slots: dict[str, list[Recipe]]) -> list[dict]:
    breakfasts = slots["Breakfast"]
    lunches = slots["Lunch"]
    dinners = dinner_pool(slots["Dinner"])
    desserts = dessert_pool(slots["Dessert"])

    french = next((b for b in breakfasts if b.prefer_breakfast), None)
    other_bfast = [b for b in breakfasts if not b.prefer_breakfast]
    french_days = {0, 2, 4, 5}

    grapefruit = next((b for b in other_bfast if b.uses_half_grapefruit), None)
    gf_days: set[int] = set()
    if grapefruit:
        pair = grapefruit_pair_days(french_days)
        if pair:
            gf_days = {pair[0], pair[1]}

    shop_dinners = sorted(
        [d for d in dinners if d.is_shop],
        key=lambda x: strip_marker(x.name).lower(),
    )
    home_dinners = [d for d in dinners if not d.is_shop]
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

        if grapefruit and i in gf_days:
            b = grapefruit
        elif french and i in french_days and i not in gf_days:
            b = french
        else:
            pool_b = [
                x
                for x in (other_bfast or breakfasts)
                if not (grapefruit and x.uid == grapefruit.uid)
            ] or other_bfast or breakfasts
            exclude = set()
            if french:
                exclude.add(french.uid)
            if grapefruit:
                exclude.add(grapefruit.uid)
            b = pick(
                pool_b,
                used_week=used_b,
                used_yesterday=yday_b,
                need_not_bread=False,
                exclude=exclude,
            )

        l = pick(
            lunches,
            used_week=used_l,
            used_yesterday=yday_l,
            need_not_bread=b.is_bread,
        )

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
                "lunch_snack": None,
                "soup": None,
                "pantry_uses": {},
            }
        )
    return plan


def _find_lunch(lunches: list[Recipe], *needles: str) -> Recipe | None:
    for needle in needles:
        nl = needle.lower()
        for r in lunches:
            if nl in strip_marker(r.name).lower():
                return r
    return None


def build_week_freezer(
    start: date, slots: dict[str, list[Recipe]], stock: dict[str, int]
) -> list[dict]:
    """Favourites breakfast/pudding + Waitrose early + freezer mains/spreads + batch night."""
    breakfasts = slots["Breakfast"]
    lunches = slots["Lunch"]
    dinners = dinner_pool(slots["Dinner"])
    desserts = dessert_pool(slots["Dessert"])

    french = next((b for b in breakfasts if b.prefer_breakfast), None)
    other_bfast = [b for b in breakfasts if not b.prefer_breakfast]
    french_days = {0, 2, 4, 5}
    grapefruit = next((b for b in other_bfast if b.uses_half_grapefruit), None)
    gf_days: set[int] = set()
    if grapefruit:
        pair = grapefruit_pair_days(french_days)
        if pair:
            gf_days = {pair[0], pair[1]}

    toast_jam = next(
        (b for b in other_bfast if "toast" in b.name.lower() and "jam" in b.name.lower()),
        None,
    )

    baked_potato = _find_lunch(lunches, "baked potato with bolognese")
    french_toasty = _find_lunch(lunches, "french toasty")
    spaghetti = _find_lunch(lunches, "spaghetti with pesto")
    sardines = _find_lunch(lunches, "sardines on toast")

    hummus = load_recipe_uid(HUMMUS_UID, slot="Lunch")
    fd_pie = load_recipe_uid(FD_PIE_UID, slot="Dinner")
    fd_bol = load_recipe_uid(FD_BOL_UID, slot="Dinner")
    fish_pie = load_recipe_uid(FD_FISH_PIE_UID, slot="Dinner") or next(
        (d for d in slots["Dinner"] if "fish pie" in d.name.lower()), None
    )
    parsnip = load_recipe_uid(PARSNIP_SOUP_UID, slot="Dinner") or next(
        (d for d in slots["Dinner"] if "parsnip soup" in d.name.lower()), None
    )

    shop_dinners = sorted(
        [d for d in dinners if d.is_shop],
        key=lambda x: strip_marker(x.name).lower(),
    )
    # Preferred Waitrose order for early week
    prefer_order = [
        "bbq pork",
        "nasi goreng",
        "pad thai",
        "teriyaki",
    ]

    def shop_rank(r: Recipe) -> tuple:
        n = strip_marker(r.name).lower()
        for i, tok in enumerate(prefer_order):
            if tok in n:
                return (i, n)
        return (len(prefer_order), n)

    shop_queue = sorted(shop_dinners, key=shop_rank)

    fruit = next(
        (d for d in desserts if "fruit salad" in d.name.lower() and "ice" in d.name.lower()),
        desserts[0] if desserts else None,
    )
    rhubarb = next(
        (d for d in desserts if "rhubarb" in d.name.lower()),
        desserts[1] if len(desserts) > 1 else fruit,
    )

    remaining = dict(stock)
    plan: list[dict] = []

    # Lunch mains by day index (bread-safe vs breakfast)
    lunch_by_day: list[Recipe | None] = [
        baked_potato,  # 0 Sat after FT
        french_toasty,  # 1 Sun
        spaghetti,  # 2 Mon after FT
        sardines,  # 3 Tue
        baked_potato,  # 4 Wed after FT
        spaghetti,  # 5 Thu after FT
        baked_potato,  # 6 Fri after toast
    ]

    for i in range(7):
        day = start + timedelta(days=i)
        pantry_uses: dict[str, int] = defaultdict(int)

        if grapefruit and i in gf_days:
            b = grapefruit
        elif french and i in french_days and i not in gf_days:
            b = french
        elif toast_jam and i not in french_days and i not in gf_days:
            b = toast_jam
        else:
            b = french or other_bfast[0]

        l = lunch_by_day[i] or pick(
            lunches, used_week=set(), used_yesterday=set(), need_not_bread=b.is_bread
        )
        if b.is_bread and l.is_bread:
            l = baked_potato or spaghetti or l

        # Cheese lunch → no hummus; else freezer hummus if stocked
        lunch_snack = None
        is_cheese_lunch = "toasty" in l.name.lower() or (
            "cheese" in l.name.lower() and "oatcake" in l.name.lower()
        )
        if (
            not is_cheese_lunch
            and hummus
            and remaining.get(HUMMUS_UID, 0) > 0
        ):
            lunch_snack = as_freezer(hummus)
            remaining[HUMMUS_UID] -= 1
            pantry_uses[HUMMUS_UID] += 1

        # Baked potato uses freezer bolognese sauce when stocked
        if (
            baked_potato
            and l.uid == baked_potato.uid
            and remaining.get(FD_BOL_UID, 0) > 0
        ):
            remaining[FD_BOL_UID] -= 1
            pantry_uses[FD_BOL_UID] += 1

        soup = None
        # Dinner schedule: Waitrose early, freezer pie/bol mid-week, batch Fri
        if i in (0, 1, 3) and shop_queue:
            d = shop_queue.pop(0)
        elif i == 2 and fd_pie and remaining.get(FD_PIE_UID, 0) > 0:
            d = as_freezer(fd_pie)
            remaining[FD_PIE_UID] -= 1
            pantry_uses[FD_PIE_UID] += 1
        elif i == 4 and fd_bol and remaining.get(FD_BOL_UID, 0) > 0:
            d = as_freezer(fd_bol)
            remaining[FD_BOL_UID] -= 1
            pantry_uses[FD_BOL_UID] += 1
        elif i == 5 and shop_queue:
            d = shop_queue.pop(0)
        elif i == 6 and fish_pie:
            d = fish_pie
            if parsnip:
                soup = parsnip
        elif shop_queue:
            d = shop_queue.pop(0)
        else:
            d = pick(
                dinners,
                used_week=set(),
                used_yesterday=set(),
                need_not_bread=l.is_bread,
            )

        p = fruit if i % 2 == 0 else (rhubarb or fruit)
        if p is None:
            p = desserts[0]

        plan.append(
            {
                "date": day,
                "breakfast": b,
                "lunch": l,
                "dinner": d,
                "pudding": p,
                "lunch_snack": lunch_snack,
                "soup": soup,
                "pantry_uses": dict(pantry_uses),
            }
        )
    return plan


def shopping_list(plan: list[dict]) -> dict[str, list[str]]:
    """Naive line merge from ingredients (skip section headers and freezer portions)."""
    counts: dict[str, int] = defaultdict(int)
    for day in plan:
        for key in ("breakfast", "lunch", "dinner", "pudding", "lunch_snack", "soup"):
            r = day.get(key)
            if not r:
                continue
            if getattr(r, "from_freezer", False):
                continue  # already cooked — do not shop
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

    def cell(r: Recipe | None, *, extra: str = "") -> str:
        if not r:
            return "—"
        mark = ""
        if r.from_freezer:
            mark += " 🧊"
        if r.is_shop:
            mark += " 🛒"
        if r.is_bread:
            mark += " 🍞"
        return strip_marker(r.name)[:40] + mark + extra

    print(f"## 7-day plan from {start.isoformat()} (Favourites + freezer)\n")
    print("| Day | Breakfast | Lunch (+ snack) | Dinner (+ soup) | Pudding |")
    print("|-----|-----------|-----------------|-----------------|---------|")
    for day in plan:
        d = day["date"].strftime("%a %d %b")
        snack = day.get("lunch_snack")
        soup = day.get("soup")
        lunch = cell(day["lunch"])
        if snack:
            lunch += " + " + cell(snack)
        dinner = cell(day["dinner"])
        if soup:
            dinner += " + " + cell(soup)
        print(
            f"| {d} | {cell(day['breakfast'])} | {lunch} | {dinner} | {cell(day['pudding'])} |"
        )

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
    print(f"Puddings used: {', '.join(sorted(puds))}")
    frozen = sum(
        1
        for d in plan
        for k in ("breakfast", "lunch", "dinner", "pudding", "lunch_snack", "soup")
        if d.get(k) and getattr(d[k], "from_freezer", False)
    )
    print(f"Freezer-sourced meal entries: {frozen}")
    uses: dict[str, int] = defaultdict(int)
    for d in plan:
        for uid, n in (d.get("pantry_uses") or {}).items():
            uses[uid] += n
    if uses:
        print("Pantry drawdown:", dict(uses))
    print()

    print("## Shopping list (merged, non-frozen mains only)\n")
    for aisle, items in shopping_list(plan).items():
        print(f"### {aisle}")
        for it in items:
            print(f"- {it}")
        print()


if __name__ == "__main__":
    main()
