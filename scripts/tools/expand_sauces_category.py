"""
Rename By type / Sauces -> Oils, Sauces, Dips and Butters
and move matching Uncategorised recipes into it.
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

NEW_NAME = "Oils, Sauces, Dips and Butters"

# Match uncategorised titles that belong in the expanded category
MATCH = re.compile(
    r"\b("
    r"sauce|salsa|dip|dressing|vinaigrette|pesto|marinade|mayo|mayonnaise|"
    r"gravy|aioli|hummus|tzatziki|chimichurri|ketchup|relish|ranch|"
    r"butter|nutella|spread|oil|infused\s*oil|seasoning|masala|jam|"
    r"guacamole|baba\s*ganoush|condiment|stock(?!\s*cube)"
    r")\b",
    re.I,
)

# Avoid false positives
EXCLUDE = re.compile(
    r"\b(burrito|quesadilla|mac\s*and\s*cheese|fried\s*rice|short\s*ribs|"
    r"egg\s*cups|hash|gnocchi|enchilada|rollatini|tortilla(?!\s*crust)|"
    r"stuffed|skewer|mimosa|popsicle|pop|tiramisu|tartlet|beignet|"
    r"profiterole|spaetzle|pierogi|spanakopita|falafel|ham|pretzel|"
    r"lady\s*fingers|spring\s*roll|frosty|cucumber\s*bites|"
    r"pigs\s*in\s*a\s*blanket|moo\s*shu|eggplant|kale\s*with|"
    r"creamed\s*spinach|mash|grilled\s*vegetables|fruit\s*skewers|"
    r"eight-ball|kadayif|kaiserschmarrn|speculaas|bruscetta|"
    r"yogurt\s*in\s*the|hard-boiled|tonic|eton\s*mess|creme|"
    r"chocolate-dipped|snowballs|clusters|balls|bars?|"
    r"rice\s*krispie|snack\s*bars?"
    r")\b",
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
            raise SystemExit(f"category sync failed: {body[:300]}")


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


def belongs(name: str) -> bool:
    if EXCLUDE.search(name):
        # Still allow clear dip/salsa/butter/oil/sauce hits even if exclude word present
        # e.g. "Crab Dip" has dip; "Tortilla Crust" dip is OK
        if re.search(
            r"\b(dip|salsa|butter|oil|sauce|dressing|seasoning|masala|"
            r"guacamole|hummus|jam|spread|nutella)\b",
            name,
            re.I,
        ):
            # But skip if it's a main dish with salsa in the name
            if re.search(
                r"\b(chicken|beef|pork|egg\s*cups|hash|quesadilla|burrito|"
                r"fried\s*rice|stuffed|bowl|taco(?!\s*sauce)|pizza)\b",
                name,
                re.I,
            ):
                return False
            return True
        return False
    return bool(MATCH.search(name))


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]

        sauces = next((c for c in cats if c.get("name") == "Sauces"), None)
        if not sauces:
            # already renamed?
            sauces = next((c for c in cats if c.get("name") == NEW_NAME), None)
        if not sauces:
            raise SystemExit("Sauces / expanded category not found")

        uncat = next(c for c in cats if c.get("name") == "Uncategorised")

        if sauces.get("name") != NEW_NAME:
            item = {
                "uid": sauces["uid"],
                "name": NEW_NAME,
                "parent_uid": sauces.get("parent_uid"),
                "order_flag": sauces.get("order_flag", 0),
            }
            await post_categories(s, headers, [item])
            safe_print(f"Renamed Sauces -> {NEW_NAME}")
        else:
            safe_print(f"Already named: {NEW_NAME}")

        sauces_uid = sauces["uid"]
        uncat_uid = uncat["uid"]
        sl, ul = sauces_uid.lower(), uncat_uid.lower()

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        sem = asyncio.Semaphore(5)
        moved = 0
        candidates: list[str] = []

        async def load(uid: str) -> dict:
            async with sem:
                for attempt in range(6):
                    async with s.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as r:
                        if r.status == 429:
                            await asyncio.sleep(1.3 * (attempt + 1))
                            continue
                        return (await r.json(content_type=None)).get("result") or {}
            return {}

        for start in range(0, len(index), 40):
            batch = await asyncio.gather(*(load(e["uid"]) for e in index[start : start + 40]))
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                cats_now = [str(c) for c in (rec.get("categories") or [])]
                if ul not in {c.lower() for c in cats_now}:
                    continue
                name = rec.get("name") or ""
                if not belongs(name):
                    continue
                candidates.append(name)
                new_cats = []
                seen = set()
                for c in cats_now:
                    repl = sauces_uid if c.lower() == ul else c
                    if repl.lower() in seen:
                        continue
                    seen.add(repl.lower())
                    new_cats.append(repl)
                if sl not in seen:
                    new_cats.append(sauces_uid)
                rec["categories"] = new_cats
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(s, headers, rec)
                if ok:
                    moved += 1
                    safe_print(f"MOVED: {name}")
                else:
                    safe_print(f"FAIL: {name}")
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.3)

        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        safe_print(f"\nCandidates matched: {len(candidates)}")
        safe_print(f"Done. moved={moved}")


if __name__ == "__main__":
    asyncio.run(main())
