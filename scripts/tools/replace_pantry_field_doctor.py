"""Replace Paprika pantry with Field Doctor cupboard staples."""
from __future__ import annotations

import asyncio
import gzip
import json
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import BACKUPS_DIR, PAPRIKA_API, paprika_credentials  # noqa: E402
from paprika_grocery_aisles import normalize_aisle  # noqa: E402

# Clean supermarket-facing pantry names + aisle (Paprika aisle strings).
# Sourced from docs/fd_cupboard_staples.json across 34 Field Doctor-Style recipes.
PANTRY_ITEMS: list[tuple[str, str]] = [
    # Salt & pepper
    ("Sea Salt", "Spices and Seasonings"),
    ("Black Pepper", "Spices and Seasonings"),
    ("Himalayan Salt", "Spices and Seasonings"),
    ("Smoked Sea Salt", "Spices and Seasonings"),
    ("Celery Salt", "Spices and Seasonings"),
    # Spices
    ("Smoked Paprika", "Spices and Seasonings"),
    ("Sweet Noble Paprika", "Spices and Seasonings"),
    ("Ground Coriander", "Spices and Seasonings"),
    ("Ground Ginger", "Spices and Seasonings"),
    ("Ground Cinnamon", "Spices and Seasonings"),
    ("Ground Cumin", "Spices and Seasonings"),
    ("Cumin Seeds", "Spices and Seasonings"),
    ("Chipotle Chilli", "Spices and Seasonings"),
    ("Ground Fennel", "Spices and Seasonings"),
    ("Ground Nutmeg", "Spices and Seasonings"),
    ("Turmeric", "Spices and Seasonings"),
    ("Ground Cardamom", "Spices and Seasonings"),
    ("Chilli Powder", "Spices and Seasonings"),
    ("Ground Fenugreek", "Spices and Seasonings"),
    ("Garam Masala", "Spices and Seasonings"),
    ("Saffron", "Spices and Seasonings"),
    ("Persian Spice Mix", "Spices and Seasonings"),
    # Herbs
    ("Mixed Herbs", "Spices and Seasonings"),
    ("Dried Oregano", "Spices and Seasonings"),
    ("Basil", "Spices and Seasonings"),
    ("Curry Leaves", "Spices and Seasonings"),
    ("Chives", "Produce"),
    # Thickeners & flours
    ("Tapioca Starch", "Baking Goods"),
    ("Oat Flour (Gluten Free)", "Baking Goods"),
    ("Quinoa Flour", "Baking Goods"),
    # Condiments & sauces
    ("Tomato Puree", "Canned and Jar Goods"),
    ("Tamari", "Sauces and Condiments"),
    ("Red Wine Vinegar", "Oils and Dressings"),
    ("White Miso", "International Cuisine"),
    ("Lemongrass", "Produce"),
    ("Tamarind Extract", "International Cuisine"),
    ("Yeast Extract", "Sauces and Condiments"),
    # Oils
    ("Extra Virgin Olive Oil", "Oils and Dressings"),
    # Plant milks & liquids
    ("Soy Milk", "Beverages"),
    ("Coconut Milk", "Canned and Jar Goods"),
    ("Coconut Milk Powder", "Baking Goods"),
    # Sweeteners
    ("Brown Sugar", "Baking Goods"),
    ("Brown Rice Syrup", "Baking Goods"),
    # Baking / bar extras
    ("Cocoa Powder", "Baking Goods"),
    ("Vanilla Extract", "Baking Goods"),
    ("Vanilla Powder", "Baking Goods"),
    ("Banana Powder", "Baking Goods"),
    ("Banana Oil", "Baking Goods"),
    ("Lemon Oil", "Baking Goods"),
    ("Coffee Powder", "Beverages"),
    # Cheese
    ("Parmigiano", "Dairy"),
]


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_pantry(session: aiohttp.ClientSession, headers: dict, items: list[dict]) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_json(items),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers, data=form) as r:
        body = await r.text()
        ok = '"result":true' in body.replace(" ", "")
        if not ok:
            print("POST failed:", body[:300])
        return ok


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            existing = (await r.json())["result"]
        print(f"Existing pantry items: {len(existing)}")

        # Backup current pantry before clearing
        stamp_dir = BACKUPS_DIR / "paprika-pantry"
        stamp_dir.mkdir(parents=True, exist_ok=True)
        backup_path = stamp_dir / "before-field-doctor-replace.json"
        backup_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Backup written: {backup_path}")

        # Soft-delete everything currently in pantry
        if existing:
            to_delete = [{**item, "deleted": True} for item in existing]
            # batch in chunks to be safe
            ok = True
            for i in range(0, len(to_delete), 40):
                chunk = to_delete[i : i + 40]
                if not await post_pantry(session, headers, chunk):
                    ok = False
            if not ok:
                raise SystemExit("Failed while clearing pantry")
            async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
                remaining = (await r.json())["result"]
            print(f"After clear: {len(remaining)} items")
            if remaining:
                # retry any leftovers
                await post_pantry(session, headers, [{**i, "deleted": True} for i in remaining])
                async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
                    remaining = (await r.json())["result"]
                print(f"After retry clear: {len(remaining)} items")

        # Create Field Doctor pantry
        async with session.get(
            f"{PAPRIKA_API}/v2/sync/groceryaisles/", headers=headers
        ) as r:
            aisle_by_name = {
                (a.get("name") or ""): a.get("uid")
                for a in (await r.json()).get("result") or []
                if not a.get("deleted") and a.get("name")
            }
        misc_uid = aisle_by_name.get("Miscellaneous")

        new_items = []
        for name, aisle in PANTRY_ITEMS:
            aname = normalize_aisle(aisle)
            if aname not in aisle_by_name:
                aname = "Miscellaneous"
            new_items.append(
                {
                    "uid": str(uuid.uuid4()),
                    "ingredient": name,
                    "aisle": aname,
                    "expiration_date": None,
                    "has_expiration": False,
                    "in_stock": True,
                    "purchase_date": None,
                    "quantity": None,
                    "aisle_uid": aisle_by_name.get(aname) or misc_uid,
                    "location_uid": None,
                    "notes": "Field Doctor Low FODMAP cupboard staple",
                }
            )

        if not await post_pantry(session, headers, new_items):
            raise SystemExit("Failed to upload new pantry items")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            final = (await r.json())["result"]

        out = stamp_dir / "field-doctor-pantry.json"
        out.write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"New pantry items: {len(final)}")
        for item in sorted(final, key=lambda x: ((x.get("aisle") or ""), x.get("ingredient") or "")):
            print(f"  [{item.get('aisle')}] {item.get('ingredient')}")
        print(f"Saved list: {out}")
        print("Sync notified — pull/refresh in Paprika if needed.")


if __name__ == "__main__":
    asyncio.run(main())
