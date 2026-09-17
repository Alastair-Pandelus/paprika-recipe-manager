"""
Review Oils, Sauces, Dips and Butters: keep only meal enhancers
(oils, sauces, dips, butters, dressings, seasonings, jams, stock).
Move salads -> Sides & Salads; full meals -> Main Meals (or Breakfast/
Baking when clear); vegetable sides -> Sides & Salads.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

DRY_RUN = "--apply" not in sys.argv

OILS = "Oils, Sauces, Dips and Butters"
SIDES = "Sides & Salads"
MAIN = "Main Meals"
BREAKFAST = "Breakfast"
BAKING = "Baking & Sweet Treats"

# Explicit keep: pure condiments / enhancers (title is the sauce/dip/oil itself)
KEEP_EXACT_OR_PREFIX = re.compile(
    r"(?i)^("
    r"sauce\s*-|"  # Fodmap Tracker Sauce - …
    r".*\b("
    r"salad\s*dressing|vinaigrette|marinade|mayo|mayonnaise|"
    r"gravy|aioli|hummus|tzatziki|chimichurri|ketchup|"
    r"bbq\s*sauce|buffalo\s*sauce|pasta\s*sauce|pizza\s*sauce|"
    r"stir[- ]?fry\s*sauce|hoisin|teriyaki\s*sauce|peanut\s*sauce|"
    r"mushroom\s*cream\s*sauce|peppercorn\s*sauce|vodka\s*pasta\s*sauce|"
    r"cranberry\s*sauce|cocktail\s*sauce|marinara\s*sauce|"
    r"garlic[- ]infused|"
    r"herb\s*butter|peanut\s*butter|seed\s*butter|nutella|"
    r"guacamole|baba\s*ganoush|tapenade|pesto(?!\s+(pasta|turkey|risotto|dinner|salad|with))|"
    r"red\s*pesto|spinach\s*feta\s*dip|crab\s*dip|"
    r"red\s*pepper\s*and\s*walnut\s*dip|"
    r"seasoning|garam\s*masala|taco\s*seasoning|cajun|"
    r"chia\s*jam|chia\s*seed\s*jam|salted\s*caramel\s*sauce|"
    r"vegetable\s*stock|dill\s*pickles|fresh\s*salsa|"
    r"pineapple\s*salsa|pomegranate\s*orange\s*salsa|"
    r"strawberry,\s*blueberry,\s*and\s*cucumber\s*salsa|"
    r"maple\s*tahini\s*dressing|nutritional\s*yeast\s*dressing|"
    r"sesame\s*lime\s*dressing|maple\s*mustard\s*dressing|"
    r"caesar\s*dressing|ranch\s*dressing|tahini\s*dressing|"
    r"chipotle\s*mayo|olive\s*oil"
    r")\b"
    r")"
)

# Strong keep: title starts with Sauce - or is clearly only a condiment name
FORCE_KEEP = re.compile(
    r"(?i)^("
    r"sauce\s*-|"
    r"3x\s+low\s+fodmap\s+salad\s+dressing|"
    r"5\s+minute\s+peanut\s+sauce|"
    r"baba\s+ganoush|"
    r"creamy\s+carrot\s+hummus|"
    r"easy\s+garam\s+masala|"
    r"easy\s+homemade\s+peanut\s+butter|"
    r"fody'?s\s+easy\s+low\s+fodmap\s+gravy|"
    r"fody'?s\s+healthy\s+grilled\s+pineapple\s+salsa|"
    r"fody'?s\s+spinach\s+feta\s+dip|"
    r"fresh\s+cranberry\s+sauce|"
    r"garlic\s+and\s+onion-free\s+taco\s+seasoning|"
    r"garlic-infused\s+olive\s+oil|"
    r"garlicky\s+low\s+fodmap\s+hummus|"
    r"low\s+fodmap\s+(bbq\s+sauce|buffalo\s+sauce|caesar\s+dressing|"
    r"cajun\s+seasoning|chimichurri|garlic-infused\s+oil|gravy|"
    r"guacamole|herb\s+butter|hoisin\s+stir-fry\s+sauce|hummus|"
    r"italian\s+seasoning|ketchup|lemon\s+vinaigrette|"
    r"mushroom\s+cream\s+sauce|nutella|olive\s+tapenade|"
    r"pasta\s+sauce|peppercorn\s+sauce|pesto\s+recipe|"
    r"pesto(?!\s+(turkey|pasta|risotto|dinner|salad|with))|"
    r"pizza\s+sauce|pomegranate\s+orange\s+salsa|"
    r"raspberry\s+chia\s+jam|red\s+pepper\s+and\s+walnut\s+dip|"
    r"red\s+pesto|refrigerator\s+dill\s+pickles|salsa|"
    r"steak\s+seasoning|stir\s+fry\s+sauce|strawberry\s+chia\s+jam|"
    r"strawberry,\s*blueberry,\s*and\s*cucumber\s+salsa|"
    r"taco\s+seasoning|vegetable\s+stock|vodka\s+pasta\s+sauce)|"
    r"mayonnaise\s+dressing|"
    r"nourishing\s+maple\s+tahini|"
    r"orange\s+pesto\s+with\s+aubergine\s+pasta\s+sauce|"
    r"quick\s+strawberry\s+balsamic\s+chia|"
    r"salted\s+caramel\s+sauce|"
    r"sesame\s+lime\s+dressing|"
    r"slurpable\s+nutritional\s+yeast|"
    r"super\s+seed\s+butter|"
    r"sweet\s+potato\s+hummus|"
    r"the\s+best\s+gluten-free\s+marinade|"
    r"the\s+best\s+low\s+fodmap\s+salad\s+dressing|"
    r"10-minute\s+baked\s+hot\s+crab\s+dip"
    r")"
)

# Salads / coleslaw / carpaccio-as-salad
TO_SIDES_SALAD = re.compile(
    r"(?i)\b("
    r"salad|coleslaw|carpaccio|"
    r"baked\s+goat\s+cheese\s+with\s+greens"
    r")\b"
)

# Clear vegetable / starch sides (not sauces)
TO_SIDES_DISH = re.compile(
    r"(?i)\b("
    r"potato\s+wedges|green\s+beans|eggplant\s+fries|"
    r"sliced\s+veggies\s+with|"
    r"cilantro\s+lime\s+rice|hasselback\s+potatoes|"
    r"maple\s+dijon\s+carrots|mashed\s+parsnips|"
    r"roasted\s+root\s+veggies|roasted\s+tomatoes|"
    r"roasted\s+broccoli|"
    r"sage\s+&\s+pecan\s+low\s+fodmap\s+dressing"  # stuffing-style dressing
    r")\b"
)

TO_BREAKFAST = re.compile(r"(?i)\bomelet\b")
TO_BAKING = re.compile(r"(?i)\b(brownies|mini\s+trifles)\b")

# Full meals wrongly tagged because title mentions sauce/dip/pesto/ketchup
TO_MAIN = re.compile(
    r"(?i)\b("
    r"shakshuka|ribs?|blt|grilled\s+cheese|gordita|tostadas?|"
    r"chicken\s+strips|turkey\s+burger|burgers?|stuffed|"
    r"shells|salmon|fish\s+filet|steak|sloppy\s+joes?|"
    r"kabobs?|fried\s+rice|nacho|crunchwrap|nuggets|"
    r"lamb\s+chops|gnocchi|quesadillas?|sliders|"
    r"meatballs?|swordfish|ham\b|tacos?|enchiladas?|"
    r"fajitas|migas|k[oö]fte|kebabs?|cod\s+fish|"
    r"mussels|nasi\s+goreng|pasta\s+casserole|"
    r"pasta\s+pesto|risotto|spaghetti|zoodle|"
    r"pesto\s+dinner|rice\s+paper\s+rolls|"
    r"polenta\s+with|sandwich|arrabbiata\s+pasta\s+sauce\s+with|"
    r"shrimp\s+marinara|turkey\s+meatballs|"
    r"pesto\s+turkey|with\s+sausage|sloppy\s+joes?|"
    r"philly\s+cheesesteak"
    r")\b"
)


def classify(name: str) -> str | None:
    """Return target type name, or None to keep in Oils/Sauces."""
    # Combo plates that mention salad but are clearly mains
    if re.search(
        r"(?i)(shrimp\s+marinara|zoodle\s+spaghetti|pasta\s+pesto\s+with\s+carpaccio)",
        name,
    ):
        return MAIN

    if FORCE_KEEP.search(name):
        # Still move if it's clearly a salad/meal that slipped into keep patterns
        if TO_SIDES_SALAD.search(name) and not re.search(
            r"(?i)\b(dressing|vinaigrette)\b", name
        ):
            # "salad dressing" stays; "tofu salad with dressing" moves
            if re.search(r"(?i)\bsalad\s+dressing\b", name):
                return None
            return SIDES
        return None

    if TO_BAKING.search(name):
        return BAKING
    if TO_BREAKFAST.search(name):
        return BREAKFAST
    # Prefer MAIN when title is clearly a meal (before salad keyword)
    if TO_MAIN.search(name) and not re.search(
        r"(?i)\b(salad|coleslaw|wedges|green\s+beans|fries|veggies\s+with|"
        r"cilantro\s+lime\s+rice|hasselback|carrots|parsnips|"
        r"roasted\s+root|roasted\s+tomatoes|roasted\s+broccoli|"
        r"sage\s+&\s+pecan)\b",
        name,
    ):
        return MAIN
    if TO_SIDES_SALAD.search(name):
        # salad dressing / vinaigrette titles stay
        if re.search(r"(?i)\b(dressing|vinaigrette)\b", name) and not re.search(
            r"(?i)\bsalad\s+with\b|\btofu\s+salad\b|\bquinoa\s+salad\b|"
            r"\bpasta\s+salad\b|\bcobb\s+salad\b|\bnoodle\s+salad\b|"
            r"\bburrata\s+(pasta\s+)?salad\b|\bspinach\s+blueberry",
            name,
        ):
            # pure dressing
            if re.search(
                r"(?i)^(sauce\s*-|.*\b(caesar|ranch|tahini|maple|mustard|"
                r"balsamic|lemon|sesame|yeast|best\s+low\s+fodmap\s+salad)\s+"
                r"dressing\b|.*vinaigrette\b)",
                name,
            ) and not re.search(r"(?i)\bsalad\s+with\b", name):
                return None
            # "X Salad with Y Dressing" -> sides
            return SIDES
        return SIDES
    if TO_SIDES_DISH.search(name):
        return SIDES
    if TO_MAIN.search(name):
        return MAIN

    # Default keep if looks like enhancer; otherwise main if looks meal-y
    if re.search(
        r"(?i)\b(sauce|dip|dressing|oil|butter|pesto|salsa|hummus|"
        r"seasoning|gravy|ketchup|mayo|marinade|jam|stock|spread)\b",
        name,
    ) and not re.search(
        r"(?i)\b(with\s+(bbq|pesto|ranch|ketchup|aioli|marinara|salsa)|"
        r"pasta\s+with|gnocchi\s+with|risotto|burger|taco|salad)\b",
        name,
    ):
        return None
    # Ambiguous leftovers that mention sauce as part of a dish
    if re.search(r"(?i)\b(with|and)\b.*\b(sauce|pesto|dip|ketchup|aioli)\b", name):
        return MAIN
    return None  # keep unknown enhancers


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


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        by_name = {c["name"]: c for c in cats}
        oils_uid = by_name[OILS]["uid"].lower()
        target_uids = {
            SIDES: by_name[SIDES]["uid"],
            MAIN: by_name[MAIN]["uid"],
            BREAKFAST: by_name[BREAKFAST]["uid"],
            BAKING: by_name[BAKING]["uid"],
        }
        oils_uid_raw = by_name[OILS]["uid"]

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        sem = asyncio.Semaphore(6)

        async def load(uid: str) -> dict:
            async with sem:
                for attempt in range(6):
                    async with s.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        return (await resp.json(content_type=None)).get("result") or {}
            return {}

        moves: list[tuple[str, str, dict]] = []
        keep: list[str] = []

        for start in range(0, len(index), 50):
            batch = await asyncio.gather(
                *(load(e["uid"]) for e in index[start : start + 50])
            )
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                cats_uids = [str(x).lower() for x in (rec.get("categories") or [])]
                if oils_uid not in cats_uids:
                    continue
                name = rec.get("name") or ""
                dest = classify(name)
                if dest is None:
                    keep.append(name)
                else:
                    moves.append((name, dest, rec))

        from collections import Counter

        c = Counter(d for _, d, _ in moves)
        safe_print(f"MODE={'APPLY' if not DRY_RUN else 'DRY-RUN'}")
        safe_print(f"In Oils/Sauces: {len(moves) + len(keep)}")
        safe_print(f"Keep: {len(keep)}")
        safe_print(f"Move: {len(moves)} -> {dict(c)}")
        safe_print("\n=== MOVES ===")
        for name, dest, _ in sorted(moves, key=lambda x: (x[1], x[0].lower())):
            safe_print(f"  [{dest}] {name}")
        safe_print("\n=== KEEP (sample / all) ===")
        for name in sorted(keep, key=str.lower):
            safe_print(f"  {name}")

        if DRY_RUN:
            safe_print("\nRe-run with --apply to write changes.")
            return

        ok = 0
        for name, dest, rec in moves:
            cats_list = list(rec.get("categories") or [])
            # remove oils uid (any case)
            cats_list = [
                x
                for x in cats_list
                if str(x).lower() != oils_uid and str(x).lower() != oils_uid_raw.lower()
            ]
            dest_uid = target_uids[dest]
            if not any(str(x).lower() == dest_uid.lower() for x in cats_list):
                cats_list.append(dest_uid)
            rec["categories"] = cats_list
            rec["hash"] = calc_hash(rec)
            if await post_recipe(s, headers, rec):
                ok += 1
                safe_print(f"moved: {name} -> {dest}")
            else:
                safe_print(f"FAIL: {name}")
        safe_print(f"\nDone. Moved {ok}/{len(moves)}")


if __name__ == "__main__":
    asyncio.run(main())
