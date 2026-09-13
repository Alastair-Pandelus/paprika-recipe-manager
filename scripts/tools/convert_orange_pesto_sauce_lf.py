"""Convert linguine dish to Low FODMAP orange pesto sauce; move to Curated."""
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
UID = "4144fbfc-9f4b-48e6-a222-c1774d7c3160"

NEW_NAME = "Orange Pesto with Aubergine Sauce (Low Fodmap Version)"
NEW_INGREDIENTS = """1 aubergine
sea salt
180ml extra virgin olive oil (for frying aubergine), plus garlic-infused oil to finish the serving bowl
lactose-free ricotta, to serve (about 2–3 tbsp per person)
freshly grated Parmigiano / Parmesan, to serve (small sprinkle)

For the orange pesto:
3 oranges, peeled, all pith and seeds removed, roughly chopped
80g basil leaves
150–200g pine nuts (Low FODMAP swap for blanched almonds — see notes)
50g capers in salt, rinsed and dried
pinch of sea salt
60ml extra virgin olive oil (more if needed to blend)

To serve:
Low FODMAP pasta of choice — e.g. rice spaghetti / rice linguine / other gluten-free rice-based pasta (enough for 4)
"""

NEW_DIRECTIONS = """This is a Sicilian-style orange and nut pesto with fried aubergine. The original used wheat linguine; here the dish is treated as a **sauce** to toss through Low FODMAP pasta.

For the pesto: place oranges, basil, pine nuts, capers, salt and olive oil in a blender. Blend on low until fine and creamy, adding a little more oil if needed. Transfer to a lidded container and cover with a thin film of olive oil. (You only need 2–3 tablespoons per serving; the rest keeps in the fridge for about 2 weeks under oil.)

Cut the ends off the aubergine, then cut into 1 cm thick slices. Place in a colander, sprinkle with sea salt and leave 40 minutes to sweat. Rinse, pat dry.

Heat the olive oil in a large frying pan until hot; fry the aubergine until golden on both sides. Drain on paper towels. When cool enough, cut into strips about 5 cm × 0.5 cm.

Cook your Low FODMAP pasta (e.g. rice spaghetti) in plenty of boiling salted water until al dente. Reserve a spoonful of cooking water.

Rub or brush the inside of a large serving bowl with garlic-infused oil (for garlic flavour without garlic cloves). Add lactose-free ricotta, aubergine strips, 2–3 tablespoons of pesto and a spoon of pasta water. Mix well. Drain the pasta, add to the bowl and toss gently, adding more pesto to taste. Finish with a little grated Parmigiano and serve.
"""

NEW_NOTES = """Low FODMAP conversion of “Linguine with orange pesto and aubergine”.

Reframed as a **sauce recipe** — serve with Low FODMAP pasta such as rice-based spaghetti or linguine (not wheat linguine or couscous).

Swaps from the original:
- Garlic clove rubbed in the bowl → garlic-infused oil
- Wheat linguine → rice spaghetti / other Low FODMAP pasta (cook separately)
- Blanched almonds (250g) → pine nuts (almonds are only Low FODMAP in small serves; a few tbsp of almond-heavy pesto can exceed that)
- Ricotta (mentioned in method but missing from the old ingredient list) → lactose-free ricotta
- Parmesan kept in a small sprinkle (usually Low FODMAP)

Orange is Low FODMAP in typical fruit serves; keep pesto to about 2–3 tbsp per person.
Aubergine, basil, capers and olive oil are Low FODMAP.
"""

NEW_DESCRIPTION = (
    "Ingredients: orange pesto (orange, basil, pine nuts, capers), aubergine, "
    "lactose-free ricotta, garlic-infused oil; serve with rice pasta\n\n"
    "Low FODMAP orange pesto and aubergine sauce for gluten-free / rice pasta."
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
                    raise SystemExit(body[:300])
            print("Deleted empty:", [c.get("name") for c in to_delete])
        else:
            print("No empty original folders")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
