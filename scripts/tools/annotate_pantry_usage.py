"""Annotate pantry item names with (common) or bracketed recipe lists."""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import DOCS_DIR, PAPRIKA_API, paprika_credentials  # noqa: E402

# Pantry display name -> staple key in fd_cupboard_staples.json
NAME_TO_KEY: dict[str, str] = {
    "Sea Salt": "sea salt",
    "Black Pepper": "black pepper",
    "Himalayan Salt": "himalayan / smoked salt",
    "Smoked Sea Salt": "himalayan / smoked salt",
    "Celery Salt": "celery salt",
    "Smoked Paprika": "paprika (smoked / sweet noble)",
    "Sweet Noble Paprika": "paprika (smoked / sweet noble)",
    "Ground Coriander": "coriander (ground)",
    "Ground Ginger": "ginger (ground)",
    "Ground Cinnamon": "cinnamon (ground)",
    "Ground Cumin": "cumin (ground)",
    "Cumin Seeds": "cumin seeds (whole)",
    "Chipotle Chilli": "chipotle chilli",
    "Ground Fennel": "fennel (ground)",
    "Ground Nutmeg": "nutmeg (ground)",
    "Turmeric": "turmeric",
    "Ground Cardamom": "cardamom (ground)",
    "Chilli Powder": "chilli powder",
    "Ground Fenugreek": "fenugreek (ground)",
    "Garam Masala": "garam masala",
    "Saffron": "saffron",
    "Mixed Herbs": "mixed herbs",
    "Dried Oregano": "oregano (dried)",
    "Basil": "basil (dried / fresh)",
    "Curry Leaves": "curry leaves",
    "Chives": "chives (fresh pack)",
    "Tapioca Starch": "tapioca flour / starch",
    "Oat Flour (Gluten Free)": "oat flour (GF)",
    "Quinoa Flour": "quinoa flour",
    "Tomato Puree": "tomato puree",
    "Tamari": "tamari / soy sauce",
    "Red Wine Vinegar": "red wine vinegar",
    "White Miso": "white miso",
    "Lemongrass": "lemongrass",
    "Tamarind Extract": "tamarind extract",
    "Yeast Extract": "yeast extract",
    "Extra Virgin Olive Oil": "extra virgin olive oil",
    "Soy Milk": "soy milk",
    "Coconut Milk": "coconut milk",
    "Coconut Milk Powder": "coconut milk powder",
    "Brown Sugar": "brown sugar",
    "Brown Rice Syrup": "brown rice syrup",
    "Cocoa Powder": "cocoa powder",
    "Vanilla Extract": "vanilla (powder / extract)",
    "Vanilla Powder": "vanilla (powder / extract)",
    "Banana Powder": "banana powder / oil",
    "Banana Oil": "banana powder / oil",
    "Coffee Powder": "coffee powder",
    "Parmigiano": "parmesan / parmigiano",
}

# Not always present in the JSON items array
EXTRA_RECIPES: dict[str, list[str]] = {
    "Persian Spice Mix": ["Moroccan Chickpea Tagine"],
    "Lemon Oil": ["Lemon Coconut Bar"],
}


def base_name(ingredient: str) -> str:
    """Strip usage qualifiers we add: (common) or (Recipe; Recipe...)."""
    s = (ingredient or "").strip()
    if s.endswith("(common)"):
        return s[: -len("(common)")].rstrip()
    # recipe list qualifier: ends with ) and contains '; ' or looks like our meal names
    if s.endswith(")") and " (" in s:
        head, _, tail = s.rpartition(" (")
        inner = tail[:-1]
        if "; " in inner or inner in {
            "Vegetable Korma",
            "Chickpea Masala Curry",
            "Field Green Risotto",
            "Chicken Pad Thai",
            "Hazelnut Mocha Bar",
            "Lemon Coconut Bar",
            "Low FODMAP Mac + Cheese",
            "Moroccan Chickpea Tagine",
        }:
            return head.strip()
        # multi-recipe without needing exact match: if any known short meal token
        if any(
            x in inner
            for x in (
                "Porridge",
                "Korma",
                "Meatballs",
                "Pie",
                "Curry",
                "Chilli",
                "Penne",
                "Bar",
                "Risotto",
                "Pad Thai",
                "Tagine",
            )
        ):
            return head.strip()
    return s


def qualify(name: str, recipes: list[str]) -> str:
    recipes = sorted(set(recipes))
    if len(recipes) > 3:
        return f"{name} (common)"
    if not recipes:
        return f"{name} (common)"  # fallback
    return f"{name} ({'; '.join(recipes)})"


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    staples = {
        i["key"]: i
        for i in json.loads((DOCS_DIR / "fd_cupboard_staples.json").read_text(encoding="utf-8"))[
            "items"
        ]
    }

    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            items = (await r.json())["result"]

        updated = []
        for item in items:
            raw = item.get("ingredient") or ""
            name = base_name(raw)
            if name in EXTRA_RECIPES:
                recipes = EXTRA_RECIPES[name]
            else:
                key = NAME_TO_KEY.get(name)
                if not key or key not in staples:
                    print(f"WARN unmapped: {raw!r} -> keeping as-is")
                    updated.append(item)
                    continue
                recipes = staples[key]["recipes"]
            new_name = qualify(name, recipes)
            updated.append({**item, "ingredient": new_name})
            print(f"{name}  ->  {new_name}")

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_json(updated),
            content_type="application/octet-stream",
            filename="data",
        )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers, data=form
        ) as r:
            body = await r.text()
            if '"result":true' not in body.replace(" ", ""):
                raise SystemExit(f"Update failed: {body[:300]}")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            final = (await r.json())["result"]

        common = sum(1 for i in final if "(common)" in (i.get("ingredient") or ""))
        specific = len(final) - common
        print(f"\nDone: {common} common, {specific} recipe-qualified, {len(final)} total")
        print("Sync notified.")


if __name__ == "__main__":
    asyncio.run(main())
