"""Remove porridge-only pantry items; keep meal staples. Does NOT touch recipes."""
from __future__ import annotations

import asyncio
import gzip
import json
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import DOCS_DIR, PAPRIKA_API, paprika_credentials  # noqa: E402

PORRIDGE_ONLY_BASE_NAMES = {
    "Oat Flour (Gluten Free)",
    "Banana Powder",
    "Banana Oil",
    "Cocoa Powder",
    "Vanilla Extract",
    "Vanilla Powder",
}

NAME_TO_KEY = {
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
    "Brown Sugar": "brown sugar",
    "Parmigiano": "parmesan / parmigiano",
}

EXTRA_RECIPES = {
    "Persian Spice Mix": ["Moroccan Chickpea Tagine"],
}


def is_bar_recipe(name: str) -> bool:
    return "Bar" in name


def is_porridge_recipe(name: str) -> bool:
    return "Porridge" in name


def meals_only(recipes: list[str]) -> list[str]:
    return sorted(
        {r for r in recipes if not is_bar_recipe(r) and not is_porridge_recipe(r)}
    )


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
                "Mac + Cheese",
            )
        ):
            return head.strip()
    return s


def qualify(name: str, recipes: list[str]) -> str:
    recipes = meals_only(recipes)
    if len(recipes) > 3:
        return f"{name} (common)"
    if not recipes:
        return name
    return f"{name} ({'; '.join(recipes)})"


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_pantry(session, headers, items: list[dict]) -> None:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_json(items),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers, data=form) as r:
        body = await r.text()
        if '"result":true' not in body.replace(" ", ""):
            raise SystemExit(f"POST failed: {body[:300]}")


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

        to_delete = []
        to_keep = []
        for item in items:
            name = base_name(item.get("ingredient") or "")
            if name in PORRIDGE_ONLY_BASE_NAMES:
                to_delete.append({**item, "deleted": True})
                print(f"Remove pantry item (porridge-only): {item.get('ingredient')}")
            else:
                to_keep.append(item)

        if to_delete:
            await post_pantry(session, headers, to_delete)

        updated = []
        for item in to_keep:
            name = base_name(item.get("ingredient") or "")
            if name in EXTRA_RECIPES:
                recipes = EXTRA_RECIPES[name]
            else:
                key = NAME_TO_KEY.get(name)
                if not key or key not in staples:
                    print(f"Keep unchanged: {item.get('ingredient')}")
                    updated.append(item)
                    continue
                recipes = staples[key]["recipes"]

            meal_recipes = meals_only(recipes)
            if not meal_recipes:
                print(f"Remove pantry item (no meal use): {item.get('ingredient')}")
                await post_pantry(session, headers, [{**item, "deleted": True}])
                continue

            new_name = qualify(name, meal_recipes)
            updated.append({**item, "ingredient": new_name})
            if new_name != item.get("ingredient"):
                print(f"Rename: {item.get('ingredient')} -> {new_name}")

        if updated:
            await post_pantry(session, headers, updated)

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            final = (await r.json())["result"]

        print(f"\nPantry items now: {len(final)} (porridge recipes untouched)")
        for i in sorted(final, key=lambda x: x.get("ingredient") or ""):
            print(f"  {i.get('ingredient')}")


if __name__ == "__main__":
    asyncio.run(main())
