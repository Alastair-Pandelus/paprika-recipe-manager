"""Add common / 3+ recipe pantry staples to Paprika groceries; tag Waitrose."""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from paprika_grocery_aisles import normalize_aisle  # noqa: E402

# Confirmed via Waitrose online search (product pages / Cooks' Ingredients / Bart etc.)
WAITROSE_AVAILABLE: set[str] = {
    "Black Pepper",
    "Brown Sugar",
    "Celery Salt",
    "Chipotle Chilli",  # Bart chipotle chilli flakes / pastes
    "Dried Oregano",
    "Extra Virgin Olive Oil",
    "Ground Cinnamon",
    "Ground Coriander",
    "Ground Cumin",
    # Ground Fennel — Waitrose has fennel seeds, not ground fennel
    "Ground Ginger",
    "Ground Nutmeg",
    "Mixed Herbs",
    "Parmigiano",
    "Red Wine Vinegar",
    "Sea Salt",
    "Smoked Paprika",
    "Sweet Noble Paprika",  # Cooks' Ingredients / Duchy paprika
    "Tamari",
    # Tapioca Starch — no Waitrose product found
    "Tomato Puree",
    "Turmeric",
    "White Miso",
}


def base_name(ingredient: str) -> str:
    s = (ingredient or "").strip()
    if s.endswith("(common)"):
        return s[: -len("(common)")].rstrip()
    if s.endswith(")") and " (" in s:
        head, _, tail = s.rpartition(" (")
        inner = tail[:-1]
        if "; " in inner or any(
            x in inner
            for x in (
                "Korma",
                "Meatballs",
                "Pie",
                "Curry",
                "Chilli",
                "Penne",
                "Risotto",
                "Pad Thai",
                "Tagine",
                "Mac + Cheese",
            )
        ):
            return head.strip()
    return s


def recipe_count(ingredient: str) -> int | None:
    """Return recipe count from pantry annotation, or None if unmarked."""
    s = (ingredient or "").strip()
    if s.endswith("(common)"):
        return 4  # annotate script uses >3 for common
    if s.endswith(")") and " (" in s:
        inner = s.rpartition(" (")[2][:-1]
        if "; " in inner or any(
            x in inner
            for x in (
                "Korma",
                "Meatballs",
                "Pie",
                "Curry",
                "Chilli",
                "Penne",
                "Risotto",
                "Pad Thai",
                "Tagine",
                "Mac + Cheese",
            )
        ):
            return inner.count("; ") + 1
    return None


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            pantry = [i for i in (await r.json())["result"] if not i.get("deleted")]

        selected: list[tuple[str, str]] = []  # display name, aisle
        for item in pantry:
            raw = item.get("ingredient") or ""
            count = recipe_count(raw)
            if count is None or count < 3:
                continue
            name = base_name(raw)
            aisle = normalize_aisle(item.get("aisle") or "Miscellaneous")
            display = f"{name} (Waitrose)" if name in WAITROSE_AVAILABLE else name
            selected.append((display, aisle))

        selected.sort(key=lambda x: x[0].lower())
        print(f"Adding {len(selected)} grocery items:")
        for name, aisle in selected:
            print(f"  [{aisle}] {name}")

        async with session.get(
            f"{PAPRIKA_API}/v2/sync/groceryaisles/", headers=headers
        ) as r:
            aisle_by_name = {
                (a.get("name") or ""): a.get("uid")
                for a in (await r.json()).get("result") or []
                if not a.get("deleted") and a.get("name")
            }
        misc_uid = aisle_by_name.get("Miscellaneous")

        groceries = []
        for i, (name, aisle) in enumerate(selected):
            aname = aisle if aisle in aisle_by_name else "Miscellaneous"
            groceries.append(
                {
                    "uid": str(uuid.uuid4()).upper(),
                    "name": name,
                    "ingredient": name,
                    "aisle": aname,
                    "aisle_uid": aisle_by_name.get(aname) or misc_uid,
                    "quantity": None,
                    "purchased": False,
                    "recipe": "",
                    "recipe_uid": None,
                    "instruction": None,
                    "separate": False,
                    "list_uid": None,
                    "order_flag": i,
                }
            )

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_json(groceries),
            content_type="application/octet-stream",
            filename="data",
        )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/groceries/", headers=headers, data=form
        ) as r:
            body = await r.text()
            if '"result":true' not in body.replace(" ", ""):
                raise SystemExit(f"groceries POST failed: {body[:300]}")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/groceries/", headers=headers) as r:
            active = [g for g in (await r.json())["result"] if not g.get("deleted")]
        print(f"\nDone. Grocery list now has {len(active)} items.")


if __name__ == "__main__":
    asyncio.run(main())
