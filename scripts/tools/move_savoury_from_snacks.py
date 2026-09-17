"""
Move savoury dishes out of Snacks & Bars into Main Meals or Sides & Salads.
Keep true snacks/bars (bars, popcorn, chips, energy balls, bark, dips-as-snack, etc.).
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

APPLY = "--apply" in sys.argv

SNACKS = "Snacks & Bars"
SIDES = "Sides & Salads"
MAIN = "Main Meals"

# Keep in Snacks & Bars
KEEP = re.compile(
    r"(?i)\b("
    r"bars?|ball|balls|bark|popcorn|chips|truffles?|snowballs|"
    r"clusters|kale\s*chips|snack\s*mix|trail\s*mix|spiced\s*nuts|"
    r"roasted\s*chickpeas|roasted\s*pepitas|energy|"
    r"protein\s*bars?|krispie|s'?mores|"
    r"chocolate\s*peanut\s*butter|peppermint\s*truffles|"
    # dips / classic party snacks that user may still want as snacks:
    # we'll move most savoury dishes; keep only clearly snack-coded FT "Snack -"
    # except meal-like Snack - items below
    r")\b"
)

# Force keep: Fodmap Tracker Snack - * that are actual snacks, plus clear snack titles
FORCE_KEEP = re.compile(
    r"(?i)^("
    r"snack\s*-\s*(buffalo\s*chicken\s*dip|cheese\s*ball|chocolate\s*bark|"
    r"deviled\s*eggs|hummus|jalape[nñ]o\s*poppers|kale\s*chips|"
    r"low-fodmap\s*energy\s*balls|low-fodmap\s*protein\s*bars|"
    r"nachos|popcorn|potato\s*skins|roasted\s*chickpeas|"
    r"snack\s*mix|spiced\s*nuts|spinach\s*dip|trail\s*mix)|"
    r"5-minute\s*buttery|"
    r"flavour\s*bomb\s*kale\s*chips|"
    r"fody'?s?\s*(chocolate\s*peppermint|dark\s*chocolate\s*chip|"
    r"gluten-free\s*pb|chocolate\s*peanut\s*butter\s*balls|"
    r"chocolate\s*truffles|peanut\s*butter\s*krispie|"
    r"s'?mores\s*bars|monster\s*rice\s*krisp|"
    r")|"
    r"low\s*fodmap\s*(carrot\s*cake\s*energy|coconut\s*snowballs|"
    r"dark\s*chocolate\s*blueberry|granola\s*bars|"
    r"passionfruit\s*yoghurt\s*bark|pizza\s*popcorn|"
    r"pumpkin\s*energy|rocher\s*energy|s'?mores\s*bars|"
    r"toffee\s*bark)|"
    r"monash\s*-\s*peanut\s*butter\s*energy|"
    r"one\s*bowl\s*low\s*fodmap\s*granola|"
    r"spicy\s*sriracha\s*roasted\s*pepitas|"
    r"fast\s*vegan\s*spinach\s*dip|"
    r"fody'?s?\s*layered\s*low\s*fodmap\s*taco\s*dip|"
    r"vegan\s*baked\s*buffalo|"
    r"warm\s*&\s*creamy\s*spinach\s*dip|"
    r"low\s*fodmap\s*deviled\s*eggs|"
    r"fody'?s?\s*low\s*fodmap\s*deviled|"
    r"snack\s*-\s*"  # any remaining Snack - kept unless forced main/side
    r")"
)

# Main meals
TO_MAIN = re.compile(
    r"(?i)\b("
    r"pizza|pasta|chicken\s*thighs|enchilada|bruschetta\s*pizza|"
    r"sheet\s*pan\s*bbq|bitterballen|"  # bitterballen could be snack - Dutch snack/appetizer
    r")\b"
)

# More carefully: mains are substantial dishes
TO_MAIN_STRICT = re.compile(
    r"(?i)("
    r"bruschetta\s*pizza|"
    r"truffle\s*pasta|"
    r"sheet\s*pan\s*bbq\s*chicken|"
    r"beef\s*enchilada\s*nachos|"  # could be snack - nachos are often snack; user said savoury -> sides/mains
    r"loaded\s*spicy\s*chicken\s*nachos|"
    r"loaded\s*low\s*fodmap\s*lentil\s*nachos|"
    r"sheet\s*pan\s*nachos|"
    r"homemade\s*low-fodmap\s*nachos|"
    r"salsa\s*verde\s*veggie\s*nachos|"
    r"low\s*fodmap\s*nachos$"
    r")"
)

# Actually for nachos - user said sides or mains. Nachos are often a meal/share plate -> Main Meals
# Croquettes, bruschetta, antipasto, mozzarella olives, prosciutto melon, crostini -> Sides
# Pizza, pasta, chicken thighs -> Main
# Egg cups already moved

TO_SIDES = re.compile(
    r"(?i)("
    r"bruschetta|crostini|croquettes?|antipasto|"
    r"prosciutto\s*melon|mozzarella\s*&\s*olives|"
    r"ricotta\s*and\s*marinara|parmesan\s*truffle\s*fries|"
    r"potato\s*croquettes|cheese\s*croquettes|mushroom\s*croquettes|"
    r"3x\s*low\s*fodmap\s*bruschetta|"
    r"cheesy\s*bruschetta|lemon\s*herbed\s*goat\s*cheese\s*bruschetta|"
    r"gut\s*healthy\s*antipasto"
    r")"
)

TO_MAIN_DISHES = re.compile(
    r"(?i)("
    r"bruschetta\s*pizza|"
    r"truffle\s*pasta|"
    r"sheet\s*pan\s*bbq\s*chicken|"
    r"nachos|"
    r"bitterballen"  # substantial Dutch snack - could be sides; treat as sides appetizer
    r")"
)


def classify(name: str) -> str | None:
    """Return MAIN, SIDES, or None to keep in snacks."""
    # Popcorn stays snack even if "pizza" flavoured
    if re.search(r"(?i)\bpopcorn\b", name):
        return None
    # Substantial protein mains
    if re.search(r"(?i)sheet\s*pan\s*bbq\s*chicken|chicken\s*thighs", name):
        return MAIN
    # Clear sides / appetizers
    if TO_SIDES.search(name) and not re.search(r"(?i)bruschetta\s*pizza", name):
        return SIDES
    # Nachos / pizza / pasta -> Main
    if re.search(r"(?i)\b(nachos|pizza|pasta)\b", name):
        return MAIN
    if re.search(r"(?i)bitterballen", name):
        return SIDES
    # Keep snack-like
    if FORCE_KEEP.search(name) or KEEP.search(name):
        if re.search(r"(?i)\b(pizza|pasta|nachos|chicken\s*thighs)\b", name):
            return MAIN
        return None
    if re.search(
        r"(?i)\b(dip|eggs|poppers|skins|hummus|cheese\s*ball)\b", name
    ):
        return None
    return SIDES


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
        snacks_uid = by_name[SNACKS]["uid"]
        sides_uid = by_name[SIDES]["uid"]
        main_uid = by_name[MAIN]["uid"]
        sul = snacks_uid.lower()
        dest_uids = {SIDES: sides_uid, MAIN: main_uid}

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

        in_snacks: list[dict] = []
        for start in range(0, len(index), 50):
            batch = await asyncio.gather(
                *(load(e["uid"]) for e in index[start : start + 50])
            )
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                if sul in {str(x).lower() for x in (rec.get("categories") or [])}:
                    in_snacks.append(rec)

        moves: list[tuple[str, str, dict]] = []
        keep: list[str] = []
        for rec in in_snacks:
            name = rec.get("name") or ""
            dest = classify(name)
            if dest is None:
                keep.append(name)
            else:
                moves.append((name, dest, rec))

        safe_print(f"MODE={'APPLY' if APPLY else 'DRY-RUN'}")
        safe_print(f"In Snacks & Bars: {len(in_snacks)}")
        safe_print(f"Keep as snack: {len(keep)}")
        safe_print(f"Move: {len(moves)}")
        safe_print("\n=== MOVE ===")
        for name, dest, _ in sorted(moves, key=lambda x: (x[1], x[0].lower())):
            safe_print(f"  [{dest}] {name}")
        safe_print("\n=== KEEP ===")
        for name in sorted(keep, key=str.lower):
            safe_print(f"  {name}")

        if not APPLY:
            safe_print("\nRe-run with --apply to write.")
            return

        ok = 0
        for name, dest, rec in moves:
            cats_now = [str(c) for c in (rec.get("categories") or [])]
            new_cats: list[str] = []
            seen: set[str] = set()
            dest_uid = dest_uids[dest]
            for c in cats_now:
                repl = dest_uid if c.lower() == sul else c
                if repl.lower() in seen:
                    continue
                seen.add(repl.lower())
                new_cats.append(repl)
            if dest_uid.lower() not in seen:
                new_cats.append(dest_uid)
            rec["categories"] = new_cats
            rec["hash"] = calc_hash(rec)
            if await post_recipe(s, headers, rec):
                ok += 1
                safe_print(f"moved: {name} -> {dest}")
            else:
                safe_print(f"FAIL: {name}")
            await asyncio.sleep(0.12)

        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        safe_print(f"\nDone. moved={ok}/{len(moves)}")


if __name__ == "__main__":
    asyncio.run(main())
