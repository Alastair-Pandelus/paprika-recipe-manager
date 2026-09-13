"""Convert Courgette & Carrot Fritters to Low FODMAP; move to Curated."""
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
UID = "E95A0A23-E6CE-4107-89ED-ECF0176134D9"

NEW_NAME = "Courgette & Carrot Fritters (Low Fodmap Version)"
NEW_INGREDIENTS = """½ courgette, grated (about 1½ cups)
1 large carrot, grated (1 heaped cup)
3 tbsp brown rice flour, or plain gluten-free flour
2–3 tbsp chives or spring onion green tops only, chopped
1 tsp Himalayan salt (or sea salt)
½ tsp black pepper, cracked
1 tsp nutritional yeast (optional)
1–2 tsp water
1–2 tsp garlic-infused olive oil (for garlic flavour — no garlic powder)
Extra virgin olive oil, for frying
1 lemon, to serve
"""

NEW_DIRECTIONS = """1. Add all fritter ingredients to a mixing bowl and mix until well combined with a moist, slightly sticky consistency. Add more water or flour as needed, and extra spices/herbs to taste.

2. Heat olive oil in a large frying pan over medium–high heat, then scoop ¼ cup of mixture into the pan and flatten with a fork. Once the underside is firm and golden, flip and cook until the other side is golden, then remove from the heat.

3. Repeat until all the mixture is cooked.

Serve with freshly squeezed lemon juice and a leafy salad. If using yoghurt or mayo, choose lactose-free yoghurt / a Low FODMAP mayo.
"""

NEW_NOTES = """Low FODMAP conversion of Zucchini & Carrot Fritters (UK naming: courgette).

Swaps:
- Zucchini → courgette (same vegetable)
- Red/brown onion or whole spring onion → chives / spring onion green tops only
- Garlic powder → garlic-infused olive oil

Courgette, carrot, rice flour and nutritional yeast (small amount) are Low FODMAP in normal serves.
If grating by hand the mix is wetter — use less water; food-processor grating may need the full water amount.
"""

NEW_DESCRIPTION = (
    "Ingredients: courgette, carrot, rice flour, chives, garlic-infused oil, lemon\n\n"
    "Low FODMAP courgette and carrot fritters — no onion bulb or garlic powder."
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

        to_delete = []
        for oc in old_cats:
            if oc.lower() == CURATED.lower():
                continue
            c = by_uid.get(oc.lower())
            if not c:
                continue
            total = direct_counts.get(oc.lower(), 0)
            print(f"check {c.get('name')}: direct={total}")
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
                    raise SystemExit(body[:300])
            print("Deleted empty:", [c.get("name") for c in to_delete])

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
