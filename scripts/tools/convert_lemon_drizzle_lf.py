"""Convert GF Lemon Drizzle to Low FODMAP and move to Curated."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

CURATED = "D48E4AA7-676D-4BE9-BB72-1E8894371385"
UID = "aac5862d-7ac5-45c9-9a54-05a9a626b188"

NEW_NAME = "Gluten-Free Lemon Drizzle Cake (Low Fodmap Version)"
NEW_INGREDIENTS = """200g butter, softened (or lactose-free butter if preferred)
200g golden caster sugar
4 eggs
175g fine polenta / cornmeal (Low FODMAP swap for ground almonds)
250g cold mashed potato
zest 3 lemons
2 tsp gluten-free baking powder
For the drizzle
4 tbsp granulated sugar
juice 1 lemon
"""

NEW_DIRECTIONS = """Heat oven to 180C/fan 160C/gas 4. Butter and line a deep, 20cm round cake tin.

Beat the sugar and butter together until light and fluffy, then gradually add the eggs, beating after each addition. Fold in the polenta, cold mashed potato, lemon zest and gluten-free baking powder.

Tip into the tin, level the top, then bake for 40–45 mins or until golden and a skewer inserted into the middle comes out clean. Turn out onto a wire rack after 10 mins cooling.

Mix the granulated sugar and lemon juice, then spoon over the top of the cake, letting it drip down the sides. Let the cake cool completely before slicing.
"""

NEW_NOTES = """Low FODMAP adaptation of a gluten-free lemon drizzle cake (BBC-style mashed-potato sponge).

FODMAP review of the original:
- Butter, sugar, eggs, mashed potato, lemon zest/juice, GF baking powder — Low FODMAP
- Ground almonds (175g in the whole cake) — only Low FODMAP in small serves (~2 tbsp / ~12g almond meal); a typical slice can go over

Conversion:
- Ground almonds → fine polenta / cornmeal (already suggested in the original for a nut-free version; corn is Low FODMAP)

Butter is Low FODMAP in normal amounts. Use lactose-free butter if you prefer.
"""

NEW_DESCRIPTION = (
    "Ingredients: butter, sugar, eggs, polenta, mashed potato, lemon, GF baking powder\n\n"
    "Low FODMAP gluten-free lemon drizzle — polenta instead of ground almonds."
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        uid_to_name = {c["uid"].lower(): c.get("name") for c in cats}
        by_uid = {c["uid"].lower(): c for c in cats}

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=headers) as r:
            recipe = (await r.json())["result"]

        old_cats = list(recipe.get("categories") or [])
        print("FOUND", recipe.get("name"))
        print("old cats:", [(uid_to_name.get(c.lower()), c) for c in old_cats])

        recipe["name"] = NEW_NAME
        recipe["ingredients"] = NEW_INGREDIENTS.strip() + "\n"
        recipe["directions"] = NEW_DIRECTIONS.strip() + "\n"
        recipe["notes"] = NEW_NOTES.strip()
        recipe["description"] = NEW_DESCRIPTION
        recipe["categories"] = [CURATED]
        recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipe["hash"] = calc_hash(recipe)

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_json(recipe),
            content_type="application/octet-stream",
            filename="data",
        )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=headers, data=form
        ) as r:
            body = await r.text()
            if '"result":true' not in body.replace(" ", ""):
                raise SystemExit(f"update failed: {body[:400]}")
        print("Updated -> Curated")

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]
        direct_counts: dict[str, int] = defaultdict(int)
        for entry in index:
            async with session.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                rec = (await r.json()).get("result") or {}
            if rec.get("in_trash"):
                continue
            for cuid in rec.get("categories") or []:
                direct_counts[cuid.lower()] += 1

        children: dict[str, list] = defaultdict(list)
        for c in cats:
            if c.get("parent_uid"):
                children[c["parent_uid"].lower()].append(c)

        def subtree_count(uid: str) -> int:
            total = direct_counts.get(uid.lower(), 0)
            for ch in children.get(uid.lower(), []):
                total += subtree_count(ch["uid"])
            return total

        to_delete = []
        for oc in old_cats:
            if oc.lower() == CURATED.lower():
                continue
            c = by_uid.get(oc.lower())
            if not c:
                continue
            total = subtree_count(oc)
            print(f"check {c.get('name')}: subtree={total}")
            if total == 0:
                to_delete.append(c)

        if to_delete:
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_json([{**c, "deleted": True} for c in to_delete]),
                content_type="application/octet-stream",
                filename="data",
            )
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
            ) as r:
                body = await r.text()
                if '"result":true' not in body.replace(" ", ""):
                    raise SystemExit(f"cat delete failed: {body[:300]}")
            print("Deleted empty:", [c.get("name") for c in to_delete])
        else:
            print("No empty original folders")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
