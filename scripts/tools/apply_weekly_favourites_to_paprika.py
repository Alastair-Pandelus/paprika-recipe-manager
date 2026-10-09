"""Apply Favourites week plan to Paprika Meals + grocery list.

Writes the calendar **Meals** planner (Meals → Week/Day) and recipe-linked
**groceries**. Does **not** create Paprika Menus.

On apply: clear existing Meals on the plan date range, then post new ones;
clear the grocery list, then recreate it from planned recipes.

  python scripts/tools/apply_weekly_favourites_to_paprika.py --start 2026-09-23
  python scripts/tools/apply_weekly_favourites_to_paprika.py --start 2026-09-23 --apply
  python scripts/tools/apply_weekly_favourites_to_paprika.py --start 2026-09-23 --apply --meals-only
  python scripts/tools/apply_weekly_favourites_to_paprika.py --start 2026-09-23 --apply --groceries-only
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fodmap_score_lib import parse_line, strip_inline_fodmap  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from paprika_grocery_aisles import aisle_for, normalize_aisle  # noqa: E402
from weekly_favourites_meal_plan import (  # noqa: E402
    Recipe,
    build_week,
    load_favourites,
    strip_marker,
)

FAV_PARENT = "01A0427C-9963-4D6B-ACAF-5EA469E6ED1C"
STAPLES_FOLDER = "Staples"
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

SLOT_ORDER = ("breakfast", "lunch", "dinner", "pudding")
SLOT_TO_MEALTYPE = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "pudding": "Dessert",
}
# Fallback numeric type if mealtypes.original_type is missing (Dessert is custom)
SLOT_TO_TYPE_INT = {
    "breakfast": 0,
    "lunch": 1,
    "dinner": 2,
    "pudding": 4,
}

# Section headers only (not "Optional: 1 lemon …" ingredient lines)
SECTION_SKIP = re.compile(
    r"^(OPTIONAL\s+\w+|SAUCE\b|TO SERVE\b|RICE\b|CHICKEN\b|PORK\b|"
    r"GREENS\b|TERIYAKI\b|BBQ\b|STICKY\b|PACK CONTAINS\b)",
    re.I,
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_entities(session, limiter, headers, endpoint: str, items: list) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(items),
        content_type="application/octet-stream",
        filename="data",
    )
    await limiter.wait_turn()
    async with session.post(
        f"{PAPRIKA_API}{endpoint}", headers=headers, data=form
    ) as r:
        txt = await r.text()
        ok = '"result":true' in txt.replace(" ", "")
        if not ok:
            safe_print(f"POST {endpoint} failed: {r.status} {txt[:400]}")
        return ok


# Cupboard staples — never add to grocery list (assume always in stock).
# Optional qty + unit before the staple name (e.g. "2 tsp sea salt", "3/8 cup hot water").
_QTY = r"(?:\d+[\/\d]*|\d*\.\d+)\s*"
_UNIT = r"(?:tsp|tbsp|teaspoons?|tablespoons?|cups?|ml|l|g|pinch(?:es)?(?:\s+of)?)\s+"
CUPBOARD_STAPLE_RE = re.compile(
    rf"^(?:{_QTY})?(?:{_UNIT})?"
    r"(?:"
    r"salt(?:\s+and\s+pepper)?|"
    r"sea\s+salt|"
    r"(?:black\s+|white\s+|ground\s+)?pepper|"
    r"(?:hot\s+|cold\s+|boiling\s+|tap\s+)?water"
    r")"
    r"(?:\s|,|$|to\s+taste|as\s+needed|for\s+cooking)",
    re.I,
)


def is_cupboard_staple_line(line: str) -> bool:
    """Salt / pepper / water — pantry defaults, not shopping items."""
    s = strip_inline_fodmap(line or "")
    s = re.sub(r"\s*[♡❣💔♥].*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return False
    # Keep real products that merely mention water/salt in the name
    low = s.lower()
    if any(
        x in low
        for x in (
            "sardine",
            "coconut water",
            "sparkling",
            "tonic",
            "rosewater",
            "salted butter",
            "celery salt",
            "garlic salt",
            "stock",
            "broth",
            "soda water",
        )
    ):
        return False
    return bool(CUPBOARD_STAPLE_RE.match(s))


def clean_ingredient_line(line: str) -> str | None:
    s = strip_inline_fodmap(line or "")
    s = re.sub(r"\[recipe:([^\]]+)\]", r"\1", s)
    s = re.sub(r"\s*//.*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s.startswith("**") or s.lower().startswith("note"):
        return None
    if s.isupper() or SECTION_SKIP.match(s):
        return None
    if len(s) < 3:
        return None
    if is_cupboard_staple_line(s):
        return None
    return s


def split_qty_name(scaled: str) -> tuple[str | None, str]:
    p = parse_line(scaled)
    if not p:
        return None, scaled
    m = re.match(
        r"^((?:pinch of\s+)|(?:\d+\s*/\s*\d+(?:\s+to\s+\d+\s*/\s*\d+)?|"
        r"\d+\s+\d+\s*/\s*\d+|\d+(?:\.\d+)?(?:\s+to\s+\d+(?:\.\d+)?)?|"
        r"\d+\s*[-–]\s*\d+)\s*(?:x\s+400\s*g\s*tins?)?"
        r"(?:\s*(?:g|kg|ml|l|tsp|tbsp|cup|cups|medium|large|small|"
        r"slices?|scoops?|handfuls?|eggs?|egg|pouches?|tins?))?)\s+(.+)$",
        scaled,
        re.I,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, scaled


def recipe_label(name: str) -> str:
    return strip_marker(name)


def load_staples_recipe() -> Recipe | None:
    """Favourites / Staples / Staples — weekly shopping-list seed (not a meal)."""
    import sqlite3

    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT r.uid, r.name, r.ingredients
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE coalesce(r.in_trash, 0) = 0
          AND c.parent_uid = ?
          AND c.name = ?
        """,
        (FAV_PARENT, STAPLES_FOLDER),
    ).fetchall()
    con.close()
    row = next(
        (r for r in rows if strip_marker(r["name"] or "").casefold() == "staples"),
        None,
    )
    if not row:
        return None
    return Recipe(
        uid=row["uid"],
        name=row["name"] or "Staples",
        slot="Staples",
        ingredients=row["ingredients"] or "",
        is_bread=False,
        is_shop=False,
        prefer_breakfast=False,
    )


def staple_product_key(text: str) -> str:
    """Normalise a grocery/staples line for de-dupe (ignore qty / 'from Staples')."""
    s = strip_inline_fodmap(text or "")
    s = re.sub(r"\s*[♡❣💔♥].*$", "", s)
    s = s.lower()
    s = re.sub(r"\([^)]*from\s+staples[^)]*\)", " ", s)
    s = re.sub(r"^\d+(?:[./]\d+)?(?:\s+\d+/\d+)?\s*", " ", s)
    s = re.sub(r"[–—\-]+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\boat cakes?\b", "oat cakes", s)
    s = re.sub(r"\bkallo corn cakes?\b", "kallo corn cakes", s)
    return s


def staples_already_covered(staple_line: str, existing_rows: list[dict]) -> bool:
    """True if a meal-plan grocery line already covers this staples seed item."""
    sk = staple_product_key(staple_line)
    if not sk:
        return False
    for row in existing_rows:
        blob = f"{row.get('name') or ''} {row.get('ingredient') or ''}"
        ek = staple_product_key(blob)
        if not ek:
            continue
        if sk == ek or sk in ek or ek in sk:
            return True
        # token overlap for short staples names (e.g. coffee, bars)
        sk_toks = set(sk.split())
        ek_toks = set(ek.split())
        if len(sk_toks) <= 2 and sk_toks and sk_toks <= ek_toks:
            return True
    return False


def append_staples_missing_only(rows: list[dict], staples: Recipe) -> None:
    """Add Staples seed lines only when not already present from planned meals."""
    for line in (staples.ingredients or "").splitlines():
        cleaned = clean_ingredient_line(line)
        if not cleaned:
            continue
        if staples_already_covered(cleaned, rows):
            safe_print(f"Staples skip (already on list): {cleaned}")
            continue
        qty, ing = split_qty_name(cleaned)
        name = f"{qty} {ing}".strip() if qty else ing
        rows.append(
            {
                "name": name,
                "ingredient": ing,
                "quantity": qty,
                "aisle": aisle_for(name, is_pack=False),
                "recipe": recipe_label(staples.name),
                "recipe_uid": staples.uid,
            }
        )


def append_recipe_grocery_rows(rows: list[dict], r: Recipe) -> None:
    label = recipe_label(r.name)
    if r.is_shop:
        ing = label
        rows.append(
            {
                "name": f"1 pack {ing}",
                "ingredient": ing,
                "quantity": "1 pack",
                "aisle": aisle_for(ing, is_pack=True),
                "recipe": label,
                "recipe_uid": r.uid,
            }
        )
        return
    for line in (r.ingredients or "").splitlines():
        cleaned = clean_ingredient_line(line)
        if not cleaned:
            continue
        qty, ing = split_qty_name(cleaned)
        name = f"{qty} {ing}".strip() if qty else ing
        rows.append(
            {
                "name": name,
                "ingredient": ing,
                "quantity": qty,
                "aisle": aisle_for(name, is_pack=False),
                "recipe": label,
                "recipe_uid": r.uid,
            }
        )


def groceries_from_plan(plan: list[dict]) -> list[dict]:
    """Grocery rows from planned recipes + Favourites Staples, then aggregated.

    Same wording (e.g. every recipe uses ``1 lemon slice``) collapses to one
    shopping line with ×N and a joined recipe list for provenance.
    Staples seed items are added **only if not already covered** by a planned
    meal ingredient (e.g. skip Staples oat cakes when Oatcakes with Cheese is on
    the plan).
    """
    rows: list[dict] = []
    for day in plan:
        for key in SLOT_ORDER:
            append_recipe_grocery_rows(rows, day[key])
    staples = load_staples_recipe()
    if staples:
        append_staples_missing_only(rows, staples)
    else:
        safe_print("WARN: Favourites/Staples recipe not found — skip staples seed")
    return aggregate_grocery_rows(rows)


def aggregate_grocery_rows(rows: list[dict]) -> list[dict]:
    """Merge rows with the same display name (case-insensitive).

    Special cases:
    - ``1 lemon slice`` → whole lemons at 4 slices per lemon (ceil).
    - ``1/2 grapefruit`` → whole grapefruits (ceil of halves / 2).
    - ``N egg`` / ``N eggs`` → sum to a single ``M eggs`` line.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    order_keys: list[str] = []
    for row in rows:
        key = (row.get("name") or "").strip().lower()
        if not key:
            continue
        # Normalise lemon-slice / grapefruit-half / egg variants onto one key
        if re.match(r"^(\d+)\s+lemon\s+slices?$", key):
            key = "__lemon_slice__"
        elif re.match(r"^(?:1\/2|½)\s+grapefruit$", key):
            key = "__grapefruit_half__"
        elif re.match(r"^(\d+)\s+eggs?$", key):
            key = "__eggs__"
        if key not in groups:
            order_keys.append(key)
        groups[key].append(row)

    out: list[dict] = []
    for i, key in enumerate(order_keys):
        items = groups[key]
        base = items[0]
        recipes: list[str] = []
        seen_r: set[str] = set()
        uids: set[str] = set()
        for it in items:
            lab = (it.get("recipe") or "").strip()
            if lab and lab not in seen_r:
                seen_r.add(lab)
                recipes.append(lab)
            uid = it.get("recipe_uid")
            if uid:
                uids.add(uid)
        recipe_label_joined = "; ".join(recipes)
        recipe_uid = items[0]["recipe_uid"] if len(uids) == 1 else None

        if key == "__lemon_slice__":
            slices = 0
            for it in items:
                m = re.match(
                    r"^(\d+)\s+lemon\s+slices?$",
                    (it.get("name") or "").strip(),
                    re.I,
                )
                slices += int(m.group(1)) if m else 1
            lemons = (slices + 3) // 4  # ceil(slices / 4)
            name = f"{lemons} lemon" if lemons == 1 else f"{lemons} lemons"
            out.append(
                {
                    "uid": str(uuid.uuid4()).upper(),
                    "name": name,
                    "ingredient": "lemon" if lemons == 1 else "lemons",
                    "quantity": str(lemons),
                    "aisle": "Fruit and Veg",
                    "aisle_uid": None,
                    "purchased": False,
                    "recipe": recipe_label_joined,
                    "recipe_uid": recipe_uid,
                    "instruction": f"{slices} slices @ 4 per lemon",
                    "separate": False,
                    "list_uid": None,
                    "order_flag": i,
                }
            )
            continue

        if key == "__grapefruit_half__":
            halves = len(items)
            wholes = (halves + 1) // 2  # ceil(halves / 2)
            name = "1 grapefruit" if wholes == 1 else f"{wholes} grapefruits"
            out.append(
                {
                    "uid": str(uuid.uuid4()).upper(),
                    "name": name,
                    "ingredient": "grapefruit" if wholes == 1 else "grapefruits",
                    "quantity": str(wholes),
                    "aisle": "Fruit and Veg",
                    "aisle_uid": None,
                    "purchased": False,
                    "recipe": recipe_label_joined,
                    "recipe_uid": recipe_uid,
                    "instruction": f"{halves} halves (use within ~2 days of cutting)",
                    "separate": False,
                    "list_uid": None,
                    "order_flag": i,
                }
            )
            continue

        if key == "__eggs__":
            total = 0
            for it in items:
                m = re.match(
                    r"^(\d+)\s+eggs?$",
                    (it.get("name") or "").strip(),
                    re.I,
                )
                total += int(m.group(1)) if m else 1
            name = f"{total} egg" if total == 1 else f"{total} eggs"
            out.append(
                {
                    "uid": str(uuid.uuid4()).upper(),
                    "name": name,
                    "ingredient": "egg" if total == 1 else "eggs",
                    "quantity": str(total),
                    "aisle": "Dairy",
                    "aisle_uid": None,
                    "purchased": False,
                    "recipe": recipe_label_joined,
                    "recipe_uid": recipe_uid,
                    "instruction": "",
                    "separate": False,
                    "list_uid": None,
                    "order_flag": i,
                }
            )
            continue

        n = len(items)
        name = base["name"]
        qty = base.get("quantity")
        ing = base.get("ingredient")
        # Paprika Amount uses ``quantity``. When the same line appears N times,
        # multiply a plain leading count (e.g. 1 fillet ×2 → Amount 2). Do not
        # multiply ranges like ``80–100 g`` / ``1–2 tbsp`` — keep ``×N`` in name.
        if n > 1:
            qm = re.match(r"^(\d+)\s*(.*)$", str(qty or "").strip())
            rest = (qm.group(2) or "").strip() if qm else ""
            is_range = bool(qm and re.match(r"^[-–—]\s*\d", rest))
            if qm and not is_range:
                total = int(qm.group(1)) * n
                qty = f"{total} {rest}".strip() if rest else str(total)
                name = f"{qty} {ing}".strip() if ing else qty
            else:
                name = f"{name}  ×{n}"
        out.append(
            {
                "uid": str(uuid.uuid4()).upper(),
                "name": name,
                "ingredient": ing,
                "quantity": qty,
                "aisle": normalize_aisle(base.get("aisle")),
                "aisle_uid": None,
                "purchased": False,
                "recipe": recipe_label_joined,
                "recipe_uid": recipe_uid,
                "instruction": "",
                "separate": False,
                "list_uid": None,
                "order_flag": i,
            }
        )
    return out


def meals_from_plan(plan: list[dict], types_by_slot: dict[str, dict]) -> list[dict]:
    """Paprika Meals calendar entries (B/L/D/pudding per day)."""
    rows: list[dict] = []
    for day in plan:
        day_str = day["date"].strftime("%Y-%m-%d 00:00:00")
        for order, key in enumerate(SLOT_ORDER, start=1):
            r: Recipe = day[key]
            mt = types_by_slot[key]
            # Built-ins use original_type 0–3. Custom types (Dessert) often have
            # original_type 0/null — use order_flag / slot fallback so they are
            # not mistaken for Breakfast.
            type_int = mt.get("original_type")
            if key == "pudding" or type_int is None or (
                mt.get("name") == "Dessert" and type_int == 0
            ):
                type_int = mt.get("order_flag")
                if type_int is None:
                    type_int = SLOT_TO_TYPE_INT[key]
            rows.append(
                {
                    "uid": str(uuid.uuid4()).upper(),
                    "recipe_uid": r.uid,
                    "date": day_str,
                    "type": int(type_int),
                    "name": r.name,
                    "order_flag": order,
                    "type_uid": mt["uid"],
                    "scale": None,
                    "is_ingredient": False,
                    "deleted": False,
                }
            )
    return rows


def meal_date_key(raw: str | None) -> str:
    """YYYY-MM-DD from Paprika meal date string."""
    if not raw:
        return ""
    return str(raw)[:10]


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--groceries-only",
        action="store_true",
        help="Refresh grocery list only; leave Meals as-is",
    )
    ap.add_argument(
        "--meals-only",
        action="store_true",
        help="Write Meals calendar only; leave groceries as-is",
    )
    args = ap.parse_args()
    apply = bool(args.apply)
    groceries_only = bool(args.groceries_only)
    meals_only = bool(args.meals_only)
    skip_groceries = meals_only
    skip_meals = groceries_only
    if groceries_only and meals_only:
        raise SystemExit("Use only one of --groceries-only / --meals-only")

    start = (
        date.fromisoformat(args.start) if args.start else date.today() + timedelta(days=1)
    )
    plan_label = f"Week of {start.isoformat()}"
    plan_dates = {(start + timedelta(days=i)).isoformat() for i in range(7)}

    slots = load_favourites()
    plan = build_week(start, slots)
    grocery_rows = groceries_from_plan(plan)

    safe_print(f"Plan: {plan_label} ({len(plan)} days) → Meals + groceries (no Menus)")
    for day in plan:
        d = day["date"].strftime("%a %d %b")
        names = " | ".join(strip_marker(day[k].name)[:36] for k in SLOT_ORDER)
        safe_print(f"  {d}: {names}")

    by_recipe: dict[str, int] = defaultdict(int)
    for g in grocery_rows:
        by_recipe[g["recipe"]] += 1
    safe_print(f"Grocery lines: {len(grocery_rows)} (linked to {len(by_recipe)} recipes)")
    for name, n in sorted(by_recipe.items(), key=lambda x: (-x[1], x[0].lower()))[:12]:
        safe_print(f"  {n:3d}  ← {name[:50]}")
    safe_print(f"Meals calendar entries: {len(plan) * len(SLOT_ORDER)}")

    if not apply:
        safe_print("Dry-run only. Pass --apply to write.")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)

    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/mealtypes/", headers=H
        )
        types_by_name = {
            t["name"]: t
            for t in (body["result"] or [])
            if not t.get("deleted") and t.get("name")
        }
        missing = [
            name
            for name in SLOT_TO_MEALTYPE.values()
            if name not in types_by_name
        ]
        if missing:
            raise SystemExit(
                f"Missing Paprika meal type(s): {', '.join(missing)}. "
                "Add them under Meal Types (expected: Breakfast, Lunch, Dinner, Dessert)."
            )
        types_by_slot = {
            slot: types_by_name[name] for slot, name in SLOT_TO_MEALTYPE.items()
        }

        # --- Meals calendar: clear plan dates, then overwrite ---
        if not skip_meals:
            meal_rows = meals_from_plan(plan, types_by_slot)
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/meals/", headers=H
            )
            existing_meals = [
                m
                for m in (body["result"] or [])
                if not m.get("deleted")
                and meal_date_key(m.get("date")) in plan_dates
            ]
            safe_print(
                f"Clearing {len(existing_meals)} Meals on "
                f"{min(plan_dates)}…{max(plan_dates)}"
            )
            if existing_meals:
                for i in range(0, len(existing_meals), 20):
                    chunk = [
                        {**m, "deleted": True} for m in existing_meals[i : i + 20]
                    ]
                    if not await post_entities(
                        s, limiter, H, "/v2/sync/meals/", chunk
                    ):
                        raise SystemExit("meals delete failed")
            safe_print(f"Adding {len(meal_rows)} Meals")
            for i in range(0, len(meal_rows), 14):
                part = meal_rows[i : i + 14]
                if not await post_entities(s, limiter, H, "/v2/sync/meals/", part):
                    raise SystemExit(f"meals add failed at {i}")
                safe_print(f"  posted meals {i + 1}-{i + len(part)}")

        # --- Groceries: clear then add with recipe provenance ---
        if not skip_groceries:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/grocerylists/", headers=H
            )
            lists = [x for x in (body["result"] or []) if not x.get("deleted")]
            default = next(
                (x for x in lists if x.get("is_default")), lists[0] if lists else None
            )
            list_uid = default["uid"] if default else None

            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/groceryaisles/", headers=H
            )
            aisle_by_name = {
                (a.get("name") or ""): a.get("uid")
                for a in (body["result"] or [])
                if not a.get("deleted") and a.get("name")
            }
            misc_uid = aisle_by_name.get("Miscellaneous")
            for g in grocery_rows:
                g["list_uid"] = list_uid
                aname = normalize_aisle(g.get("aisle"))
                if aname not in aisle_by_name:
                    aname = "Miscellaneous"
                g["aisle"] = aname
                g["aisle_uid"] = aisle_by_name.get(aname) or misc_uid

            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/groceries/", headers=H
            )
            existing = [g for g in (body["result"] or []) if not g.get("deleted")]
            safe_print(f"Clearing {len(existing)} existing groceries")
            if existing:
                for i in range(0, len(existing), 20):
                    chunk = [
                        {**g, "deleted": True, "purchased": False}
                        for g in existing[i : i + 20]
                    ]
                    if not await post_entities(
                        s, limiter, H, "/v2/sync/groceries/", chunk
                    ):
                        raise SystemExit("grocery delete failed")

            by_aisle: dict[str, int] = defaultdict(int)
            for g in grocery_rows:
                by_aisle[g.get("aisle") or "?"] += 1
            safe_print(
                "Aisles: "
                + ", ".join(f"{k}={v}" for k, v in sorted(by_aisle.items()))
            )

            safe_print(f"Adding {len(grocery_rows)} groceries (recipe-linked)")
            for i in range(0, len(grocery_rows), 20):
                part = grocery_rows[i : i + 20]
                if not await post_entities(s, limiter, H, "/v2/sync/groceries/", part):
                    raise SystemExit(f"grocery add failed at {i}")
                safe_print(f"  posted groceries {i + 1}-{i + len(part)}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        if not skip_meals:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/meals/", headers=H
            )
            live_meals = [
                m
                for m in (body["result"] or [])
                if not m.get("deleted")
                and meal_date_key(m.get("date")) in plan_dates
            ]
            by_day: dict[str, list[str]] = defaultdict(list)
            uid_to_slot = {
                (types_by_slot[k].get("uid") or "").upper(): k for k in SLOT_ORDER
            }
            slot_letter = {
                "breakfast": "B",
                "lunch": "L",
                "dinner": "D",
                "pudding": "P",
            }
            for m in sorted(
                live_meals,
                key=lambda x: (meal_date_key(x.get("date")), x.get("order_flag") or 0),
            ):
                slot = uid_to_slot.get((m.get("type_uid") or "").upper(), "?")
                lab = slot_letter.get(slot, "?")
                by_day[meal_date_key(m.get("date"))].append(
                    f"{lab}:{(m.get('name') or '')[:28]}"
                )
            safe_print(f"Done | Meals on plan dates={len(live_meals)}")
            for d in sorted(by_day):
                safe_print(f"  {d}: {' | '.join(by_day[d])}")
        if not skip_groceries:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/groceries/", headers=H
            )
            active_g = [g for g in (body["result"] or []) if not g.get("deleted")]
            aisle_counts: dict[str, int] = defaultdict(int)
            for g in active_g:
                aisle_counts[g.get("aisle") or "?"] += 1
            safe_print(f"Done | groceries={len(active_g)}")
            for a, n in sorted(aisle_counts.items()):
                safe_print(f"  {a}: {n}")


if __name__ == "__main__":
    asyncio.run(main())
