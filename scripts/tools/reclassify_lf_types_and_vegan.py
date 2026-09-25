"""
Reclassify Low Fodmap recipes with dual categories:
  - keep existing website/source folders
  - add Type / <9 types>
  - add Diet / Vegan only when title explicitly says vegan or plant-based
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

BY_TYPE = "Type"
DIET = "Diet"
VEGAN = "Vegan"
UNCATEGORISED = "Uncategorised"

TYPES = [
    "Main Meals",
    "Soups",
    "Sides & Salads",
    "Breakfast",
    "Baking & Sweet Treats",
    "Snacks & Bars",
    "Smoothies and Drinks",
    "Oils, Sauces, Dips and Butters",
    "Ice Cream and Frozen Desserts",
    UNCATEGORISED,
]

FT_PREFIX = {
    "meal": "Main Meals",
    "soup": "Soups",
    "side": "Sides & Salads",
    "breakfast": "Breakfast",
    "sweet": "Baking & Sweet Treats",
    "sauce": "Oils, Sauces, Dips and Butters",
    "smoothie": "Smoothies and Drinks",
    "mocktail": "Smoothies and Drinks",
    "snack": "Snacks & Bars",
}

EXPLICIT_VEGAN = re.compile(r"\b(vegan|plant[\s-]?based)\b", re.I)

RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "Ice Cream and Frozen Desserts",
        re.compile(
            r"\b(ice\s*cream|gelato|sorbet|frozen\s*yogurt|popsicle|ice\s*lolly|"
            r"magnum|nice\s*cream|frozen\s*dessert)\b",
            re.I,
        ),
    ),
    (
        "Smoothies and Drinks",
        re.compile(
            r"\b(smoothie|mocktail|latte|lemonade|iced\s*(coffee|matcha)|"
            r"hot\s*chocolate|turmeric\s*latte|matcha\s*soda|ginger\s*tea|"
            r"eggnog|margarita|mojito|sangria|pina\s*colada|cider)\b",
            re.I,
        ),
    ),
    (
        "Snacks & Bars",
        re.compile(
            r"\b(granola\s*bars?|protein\s*bars?|energy\s*bars?|energy\s*balls?|"
            r"snack\s*bars?|krispie\s*bars?|pb\s*&\s*j\s*bars?|"
            r"s'?mores\s*bars?|snack\s*mix|trail\s*mix|popcorn|kale\s*chips|nachos|"
            r"cheese\s*ball|deviled\s*eggs|jalape[nñ]o\s*poppers|"
            r"potato\s*skins|spiced\s*nuts|roasted\s*chickpeas|pepitas|"
            r"chocolate\s*bark|toffee\s*bark|yogurt\s*bark|yoghurt\s*bark|"
            r"buffalo\s*chicken\s*dip|spinach\s*dip|layer(?:ed)?\s*.*dip|"
            r"rice\s*krispie|no[\s-]?bake\s*bars?|truffles?|"
            r"peanut\s*butter\s*balls|mac\s*nut\s*clusters|coconut\s*snowballs|"
            r"marinated\s*.*mozzarella|bruschetta(?!\s+pasta)|crostini|"
            r"appetizer|croquettes?)\b",
            re.I,
        ),
    ),
    (
        "Oils, Sauces, Dips and Butters",
        re.compile(
            r"\b(pizza\s*sauce|pasta\s*sauce|salad\s*dressing|vinaigrette|"
            r"marinade|hummus|tzatziki|chimichurri|ketchup|aioli|"
            r"taco\s*seasoning|bbq\s*sauce|stir[\s-]?fry\s*sauce|"
            r"cranberry\s*sauce|cocktail\s*sauce|chia\s*jam|jam\b|"
            r"garlic[\s-]?infused\s*olive\s*oil|vegetable\s*stock|"
            r"baba\s*ganoush|guacamole|butter|nutella|spread|"
            r"peanut\s*butter|seed\s*butter|herb\s*butter|"
            r"infused\s*oil|garam\s*masala|salsa|dip)\b|"
            r"^(?=.*\b(sauce|dressing|gravy|seasoning|mayo|mayonnaise|"
            r"pesto|condiment|pickles?|ranch|oil|butter|dip|salsa)\b)"
            r"(?!.*\b(chicken|beef|pork|salmon|shrimp|pasta\s+with|"
            r"taco|burger|pizza\s+with|bowl|skillet|sheet|casserole|"
            r"curry|stir[\s-]?fry|soup|lasagna|quesadilla|burrito|"
            r"fried\s*rice|egg\s*cups|hash)\b).*$",
            re.I,
        ),
    ),
    (
        "Soups",
        re.compile(
            r"\b(soup|chowder|broth|stock|gazpacho|bisque|pho|ramen|"
            r"minestrone|zuppa|stew|chili|chilli|dahl|dal)\b",
            re.I,
        ),
    ),
    (
        "Breakfast",
        re.compile(
            r"\b(breakfast|overnight\s*oats|porridge|pancakes?|waffles?|"
            r"french\s*toast|scramble|frittata|parfait|chia\s*pudding|"
            r"egg\s*muffin|bagels?|hash\s*browns?|crepes?|eggs\s*benedict|"
            r"granola(?!\s*bar)|oatmeal(?!\s*cookie)|steel[\s-]?cut\s*oats|"
            r"pumpkin\s*spice\s*oats|"
            r"breakfast\s*(burrito|casserole|sandwich|cookies?|bowl|hash))\b",
            re.I,
        ),
    ),
    (
        "Baking & Sweet Treats",
        re.compile(
            r"\b(muffins?|cookies?|biscuits?|cakes?|cupcakes?|scones?|"
            r"brownies?|blondies?|shortbread|donuts?|doughnuts?|loaf|"
            r"cinnamon\s*rolls?|pie|tart|crumble|cobbler|cracker|"
            r"bread(?!\s*pudding)|cornbread|naan|dinner\s*rolls?|"
            r"garlic\s*bread|chapati|flatbread|buns?\b|swiss\s*roll|"
            r"fudge|panna\s*cotta|trifle|mousse|pavlovas?|"
            r"cheesecake|custard|sugar\s*cookies?|lemon\s*bars?|"
            r"baked\s*oatmeal|chocolate\s*balls?|chokladbollar|"
            r"snickerdoodles?|funfetti)\b",
            re.I,
        ),
    ),
    (
        "Sides & Salads",
        re.compile(
            r"\b(side|salad|slaw|coleslaw|fries|fritters?|tabbouleh|"
            r"mashed\s*potatoes|pommes\s*duchesse|polenta|"
            r"rice\s*pilaf|basmati\s*rice|stuffing|"
            r"green\s*bean\s*casserole|scalloped\s*potatoes|"
            r"smashed\s*potatoes|sweet\s*potato\s*(fries|casserole)|"
            r"caprese|potato\s*salad|pasta\s*salad|broccoli\s*salad|"
            r"root\s*vegetables|grilled\s*veggies|roasted\s*veg|"
            r"roasted\s*(carrots|potatoes|brussels|parsnips|broccoli|"
            r"squash|acorn|kabocha|vegetables)|"
            r"ratatouille(?!\s+.*enchilada)|spaghetti\s*squash)\b",
            re.I,
        ),
    ),
    (
        "Main Meals",
        re.compile(
            r"\b(chicken|beef|pork|salmon|tofu|tempeh|shrimp|turkey|tuna|"
            r"haddock|fish|pasta|penne|curry|korma|tacos?|burrito|"
            r"enchilada|bowl|stir[\s-]?fry|lasagna|casserole|risotto|"
            r"pizza|burgers?|sliders?|meatballs?|fajita|sheet[\s-]?pan|"
            r"skillet|roast|pad\s*thai|pad\s*see\s*ew|bolognese|alfredo|"
            r"dinner|lunch|schnitzel|moussaka|rendang|cordon\s*bleu|"
            r"wings|spareribs|tostadas|quesadilla|fish\s*fingers|"
            r"scallops|stuffed\s*(peppers|zucchini|vine)|"
            r"wraps?|sandwich(?!\s*cookie)|yaprak|sarma|"
            r"mediterranean\s*fish|vegetable\s*stuffed|"
            r"quinoa\s*veggie|bacon)\b",
            re.I,
        ),
    ),
]

SAUCE_FORCE = re.compile(
    r"\b(pizza\s*sauce|pasta\s*sauce|salad\s*dressing|vinaigrette|"
    r"marinade|hummus|tzatziki|pesto(?!\s+pasta)|gravy|ketchup|"
    r"chimichurri|ranch\s*dressing|garlic[\s-]?infused\s*olive\s*oil|"
    r"taco\s*seasoning|bbq\s*sauce|stir[\s-]?fry\s*sauce|"
    r"cranberry\s*sauce|cocktail\s*sauce|chia\s*jam)\b",
    re.I,
)


def safe_print(*a, **k) -> None:
    try:
        print(*a, **k, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), **k, flush=True)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def classify_type(name: str, description: str = "") -> str:
    m = re.match(
        r"^(Meal|Soup|Breakfast|Sauce|Smoothie|Mocktail|Sweet|Snack|Side)\s*[-–—]\s*",
        name,
        re.I,
    )
    if m:
        return FT_PREFIX[m.group(1).lower()]

    if SAUCE_FORCE.search(name) and not re.search(
        r"\b(chicken|beef|pork|salmon|shrimp|tofu|pasta\s+with|pizza\s+with)\b",
        name,
        re.I,
    ):
        return "Oils, Sauces, Dips and Butters"

    for typ, rx in RULES:
        if rx.search(name):
            return typ
    desc = (description or "")[:300]
    if desc:
        for typ, rx in RULES:
            if rx.search(desc):
                return typ
    return UNCATEGORISED


def is_explicit_vegan(name: str) -> bool:
    return bool(EXPLICIT_VEGAN.search(name or ""))


async def post_categories(session, headers, items: list[dict]) -> None:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(items), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
    ) as r:
        body = await r.text()
        if '"result":true' not in body.replace(" ", ""):
            raise SystemExit(f"category sync failed: {body[:400]}")


async def post_recipe(session, headers, recipe: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


async def ensure_child(
    session, headers, cats: list[dict], name: str, parent_uid: str, order: int
) -> str:
    existing = next(
        (
            c
            for c in cats
            if (c.get("name") or "") == name
            and (c.get("parent_uid") or "").lower() == parent_uid.lower()
            and not c.get("deleted")
        ),
        None,
    )
    if existing:
        return existing["uid"]
    uid = str(uuid.uuid4()).upper()
    item = {"uid": uid, "name": name, "parent_uid": parent_uid, "order_flag": order}
    await post_categories(session, headers, [item])
    cats.append(item)
    safe_print(f"Created: {name} ({uid})")
    return uid


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as paprika:
        async with paprika.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with paprika.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]

        by_type_uid = await ensure_child(paprika, headers, cats, BY_TYPE, LF, 50)
        diet_uid = await ensure_child(paprika, headers, cats, DIET, LF, 51)
        vegan_uid = await ensure_child(paprika, headers, cats, VEGAN, diet_uid, 0)

        type_uids: dict[str, str] = {}
        for i, name in enumerate(TYPES):
            type_uids[name] = await ensure_child(
                paprika, headers, cats, name, by_type_uid, i
            )

        # Website/source folders under LF (exclude our new structural folders)
        structural = {BY_TYPE.lower(), DIET.lower(), VEGAN.lower(), *[t.lower() for t in TYPES]}
        source_uids = {
            c["uid"].lower()
            for c in cats
            if (c.get("parent_uid") or "").lower() == LF.lower()
            and (c.get("name") or "").lower() not in structural
            and (c.get("name") or "") != "Needs LF review"
        }
        # also treat recipes that somehow only have type/diet — still OK
        safe_print(f"Source folders: {len(source_uids)}")

        async with paprika.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        type_counts: Counter[str] = Counter()
        vegan_n = 0
        updated = failed = skipped = 0
        sem = asyncio.Semaphore(4)

        async def load(uid: str) -> dict | None:
            async with sem:
                for attempt in range(6):
                    async with paprika.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as r:
                        if r.status == 429:
                            await asyncio.sleep(1.4 * (attempt + 1))
                            continue
                        return (await r.json(content_type=None)).get("result") or {}
            return None

        recipes: list[dict] = []
        for start in range(0, len(index), 40):
            batch = await asyncio.gather(*(load(e["uid"]) for e in index[start : start + 40]))
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                cats_l = {str(c).lower() for c in (rec.get("categories") or [])}
                if cats_l & source_uids:
                    recipes.append(rec)
            await asyncio.sleep(0.35)
        safe_print(f"LF source recipes to reclassify: {len(recipes)}")

        type_uid_set = {u.lower() for u in type_uids.values()}
        type_uid_set.add(by_type_uid.lower())
        diet_uid_set = {diet_uid.lower(), vegan_uid.lower()}

        for i, rec in enumerate(recipes, 1):
            name = rec.get("name") or ""
            typ = classify_type(name, rec.get("description") or "")
            vegan = is_explicit_vegan(name)
            type_counts[typ] += 1
            if vegan:
                vegan_n += 1

            old_cats = [str(c) for c in (rec.get("categories") or [])]
            # Keep source folders; drop previous Type / Diet tags then re-add
            kept = [
                c
                for c in old_cats
                if c.lower() not in type_uid_set and c.lower() not in diet_uid_set
            ]
            new_cats = kept + [type_uids[typ]]
            if vegan:
                new_cats.append(vegan_uid)

            # normalize order unique
            seen = set()
            final = []
            for c in new_cats:
                cl = c.lower()
                if cl in seen:
                    continue
                seen.add(cl)
                final.append(c)

            if {c.lower() for c in old_cats} == {c.lower() for c in final}:
                skipped += 1
                continue

            rec["categories"] = final
            rec["hash"] = calc_hash(rec)
            ok = await post_recipe(paprika, headers, rec)
            if ok:
                updated += 1
                if i % 50 == 0 or vegan:
                    tag = f" type={typ}" + (" vegan" if vegan else "")
                    safe_print(f"[{i}/{len(recipes)}] OK {name[:70]}{tag}")
            else:
                failed += 1
                safe_print(f"[{i}/{len(recipes)}] FAIL {name}")
            await asyncio.sleep(0.2)

        async with paprika.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        safe_print("\n=== Type counts ===")
        for t in TYPES:
            safe_print(f"  {type_counts.get(t, 0):4d}  {t}")
        safe_print(f"\nExplicit Vegan tagged: {vegan_n}")
        safe_print(f"Updated={updated} skipped_unchanged={skipped} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
