"""Convert Aloo Tikki to Low FODMAP and move to Curated."""
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
UID = "b60001ca-706a-4ac6-8ed3-60013dc2b552"

NEW_NAME = "Aloo Tikki (Low Fodmap Version)"
NEW_INGREDIENTS = """300 grams potatoes (OR 3 medium sized potatoes)
½ teaspoon kashmiri red chilli powder (OR 1 to 2 green chilies, finely chopped)
½ teaspoon coriander powder (dhania powder)
½ teaspoon cumin powder (jeera powder)
½ teaspoon dry ginger powder (saunth) or ½ tsp minced fresh ginger
½ teaspoon chaat masala powder (onion- and garlic-free; check label)
1.5 to 2 tablespoon cornflour / cornstarch or rice flour or arrowroot flour (add as required) — do not use wheat plain flour
2 to 3 teaspoons chopped coriander leaves
2 tablespoons oil (for frying the tikkis, or as required)
salt (as required)
"""

NEW_DIRECTIONS = """Preparation

1. Boil the potatoes until they are falling apart. Drain well.
2. Mash the potatoes.
3. Add the spice powders, salt, cornflour/rice flour/arrowroot, and coriander leaves. Mix well.
4. With your hands, form balls from the potato mix, each weighing around 45 g. Flatten into patties. Apply a little oil on your palms if sticky. Refrigerate for 20 minutes to firm up.

Frying

5. Heat oil until medium-hot in a tava or shallow frying pan. Gently place the aloo tikki and pan-fry.
6. When one side is golden and crisp, turn and fry the other side, flipping a couple of times until evenly golden and crisp.
7. Drain on paper towels to remove excess oil. Serve warm.

https://www.youtube.com/watch?v=-zGPAWG34tE
"""

NEW_NOTES = """Low FODMAP adaptation of Veg Recipes of India–style aloo tikki / potato cutlets.

This recipe was already mostly Low FODMAP (potato + dry spices + coriander). Changes:
- Bind with cornflour, rice flour, or arrowroot only — not wheat plain flour
- Use onion- and garlic-free chaat masala (many blends are fine; check for onion/garlic powder and wheat-based hing)

No onion or garlic in this version. Potatoes and the listed spices are Low FODMAP in normal amounts.
"""

NEW_DESCRIPTION = (
    "Ingredients: potato, chilli, coriander, cumin, ginger, chaat masala, cornflour, oil\n\n"
    "Low FODMAP potato tikki / cutlet — no onion, garlic, or wheat."
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
                raise SystemExit(f"recipe update failed: {body[:400]}")
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
                    raise SystemExit(f"category delete failed: {body[:300]}")
            print("Deleted empty:", [c.get("name") for c in to_delete])
        else:
            print("No empty original folders")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
