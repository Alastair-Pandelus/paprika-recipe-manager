"""Fix and convert Slow-Cooker Pulled Pork to Low FODMAP; move to Curated."""
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
UID = "a78d2881-fb5a-432c-986d-615cf8a52ad1"

NEW_NAME = "Slow-Cooker Pulled Pork with Chipotle, Maple and Lime (Low Fodmap Version)"
NEW_INGREDIENTS = """¼ cup water (or onion- and garlic-free stock)
1 boneless pork shoulder roast (about 3 lb / 1.4 kg)
1½ teaspoons salt
1½ teaspoons black pepper
1–2 teaspoons chipotle chilli powder or flakes (or dried chipotle, rehydrated and chopped — avoid canned chipotle in adobo unless the sauce is onion- and garlic-free)
½ cup tomato puree / passata mixed with a little maple syrup and vinegar to taste (onion- and garlic-free ketchup alternative)
3 tablespoons lime juice
2 tablespoons tamari (or onion- and garlic-free Worcestershire-style sauce if you have one)
2 tablespoons maple syrup (Low FODMAP swap for honey)
2 tablespoons butter, melted (or lactose-free butter)
1 teaspoon ground coriander
2–3 tablespoons chives or spring onion green tops only, chopped (optional, for finishing)
"""

NEW_DIRECTIONS = """1. Place the water (or stock) in the slow cooker. Sprinkle the pork with salt, pepper and half of the chipotle. Add the pork to the slow cooker.

2. Cover and cook on Low for 6–8 hours, until the pork is very tender.

3. Remove the pork to a large plate or board; cool slightly. Discard the cooking juices (or skim and keep a little if you like). Shred the pork with two forks; discard excess fat. Return the shredded pork to the slow cooker.

4. In a small bowl, mix the remaining chipotle, tomato-maple mixture (or onion/garlic-free ketchup), lime juice, tamari, maple syrup, melted butter and coriander. Stir into the pork. Add chives / spring onion greens if using.

5. Cover and cook on Low for 1 hour more, until saucy and heated through.

6. Serve with Low FODMAP sides — e.g. rice, baked potato, or gluten-free wraps (skip standard cornbread / oniony toppings unless they are Low FODMAP).
"""

NEW_NOTES = """Low FODMAP conversion of Betty Crocker Slow-Cooker Pulled Pork with Chipotle, Honey and Lime.

Ingredient list cleaned (Betty Crocker import had amounts and names on separate lines).

FODMAP issues in the original and swaps:
- Onions → omitted; optional chives / spring onion green tops only
- Honey → maple syrup
- Ketchup (often onion/garlic) → tomato puree / passata + maple + vinegar, or a labelled onion- and garlic-free ketchup
- Worcestershire (usually onion/garlic) → tamari, or a Low FODMAP Worcestershire-style sauce
- Chipotle in adobo (adobo often has onion/garlic) → chipotle powder/flakes or plain dried chipotle
- Butter kept in a small amount (usually Low FODMAP)

Pork, salt, pepper, lime and coriander are Low FODMAP.
Original source: bettycrocker.com
"""

NEW_DESCRIPTION = (
    "Ingredients: pork shoulder, chipotle, tomato/maple sauce, lime, tamari, maple, butter, coriander\n\n"
    "Low FODMAP slow-cooker pulled pork — no onion, honey, or adobo sauce."
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
                print("skip unknown cat", oc)
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

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
