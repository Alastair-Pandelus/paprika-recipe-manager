"""Create Favourites / Staples folder + Staples seed recipe (Crisps, Coffee, Bars)."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
FAV = "01A0427C-9963-4D6B-ACAF-5EA469E6ED1C"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
FOLDER = "Staples"
RECIPE_NAME = "Staples"
INGS = "Crisps\nCoffee\nBars\n"
DIRS = (
    "**Weekly staples:** Not a meal — pantry items always added to the "
    "shopping list when the Favourites week plan is applied.\n"
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def main() -> None:
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=H
        )
        cats = [c for c in (body["result"] or []) if not c.get("deleted")]
        staples_cat = next(
            (
                c
                for c in cats
                if (c.get("name") or "") == FOLDER
                and (c.get("parent_uid") or "").upper() == FAV.upper()
            ),
            None,
        )
        if staples_cat:
            cat_uid = staples_cat["uid"]
            safe_print(f"Folder exists: {cat_uid}")
        else:
            cat_uid = str(uuid.uuid4()).upper()
            item = {
                "uid": cat_uid,
                "name": FOLDER,
                "parent_uid": FAV,
                "order_flag": 4,
            }
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj([item]),
                content_type="application/octet-stream",
                filename="data",
            )
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/categories/", headers=H, data=form
            ) as r:
                txt = await r.text()
                if '"result":true' not in txt.replace(" ", ""):
                    raise SystemExit(f"category create failed: {txt[:300]}")
            safe_print(f"Created folder Staples: {cat_uid}")

        # Prefer local DB lookup; only create if missing
        con = sqlite3.connect(DB)
        existing = con.execute(
            """
            SELECT r.uid FROM recipes r
            JOIN recipes_to_categories rtc ON rtc.recipe_uid=r.uid
            WHERE rtc.category_uid=? AND r.name=? AND coalesce(r.in_trash,0)=0
            LIMIT 1
            """,
            (cat_uid, RECIPE_NAME),
        ).fetchone()
        con.close()
        recipe_uid = existing[0] if existing else None

        if recipe_uid:
            await limiter.wait_turn()
            async with s.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{recipe_uid}/", headers=H
            ) as r:
                recipe = (await r.json())["result"]
            safe_print(f"Updating recipe {recipe_uid}")
        else:
            recipe_uid = str(uuid.uuid4()).upper()
            recipe = {
                "uid": recipe_uid,
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "photo": None,
                "photo_hash": None,
                "photo_large": None,
                "image_url": None,
                "source": "",
                "source_url": "",
                "rating": 0,
                "cook_time": "",
                "prep_time": "",
                "total_time": "",
                "servings": "1",
                "difficulty": "Easy",
                "nutritional_info": "",
                "notes": (
                    "Edit this recipe’s ingredients to change weekly staples. "
                    "Always merged into the Favourites week grocery list."
                ),
                "scale": None,
                "in_trash": False,
                "is_pinned": False,
                "on_favorites": False,
            }
            safe_print(f"Creating recipe {recipe_uid}")

        recipe["name"] = RECIPE_NAME
        recipe["ingredients"] = INGS
        recipe["directions"] = DIRS
        recipe["description"] = (
            "Ingredients: crisps, coffee, bars\n\n"
            "Weekly shopping-list seed (not a meal). Always included when the "
            "Favourites week plan groceries are applied."
        )
        recipe["categories"] = [LF, cat_uid]
        recipe.pop("nutrition", None)
        recipe["nutritional_info"] = recipe.get("nutritional_info") or ""
        recipe["hash"] = calc_hash(recipe)

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(recipe),
            content_type="application/octet-stream",
            filename="data",
        )
        await limiter.wait_turn()
        async with s.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{recipe_uid}/", headers=H, data=form
        ) as r:
            txt = await r.text()
            if '"result":true' not in txt.replace(" ", ""):
                raise SystemExit(f"recipe save failed: {txt[:400]}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        # Local SQLite mirror for meal-plan script
        con = sqlite3.connect(DB)
        if not con.execute(
            "SELECT 1 FROM recipe_categories WHERE uid=?", (cat_uid,)
        ).fetchone():
            con.execute(
                "INSERT INTO recipe_categories (uid, name, parent_uid, order_flag) "
                "VALUES (?,?,?,?)",
                (cat_uid, FOLDER, FAV, 4),
            )
        else:
            con.execute(
                "UPDATE recipe_categories SET name=?, parent_uid=? WHERE uid=?",
                (FOLDER, FAV, cat_uid),
            )
        if con.execute("SELECT 1 FROM recipes WHERE uid=?", (recipe_uid,)).fetchone():
            con.execute(
                "UPDATE recipes SET name=?, ingredients=?, directions=?, description=? "
                "WHERE uid=?",
                (
                    RECIPE_NAME,
                    INGS.strip(),
                    DIRS.strip(),
                    recipe["description"],
                    recipe_uid,
                ),
            )
        else:
            con.execute(
                "INSERT INTO recipes (uid, name, ingredients, directions, description, "
                "servings, difficulty, in_trash) VALUES (?,?,?,?,?,?,?,0)",
                (
                    recipe_uid,
                    RECIPE_NAME,
                    INGS.strip(),
                    DIRS.strip(),
                    recipe["description"],
                    "1",
                    "Easy",
                ),
            )
        con.execute(
            "DELETE FROM recipes_to_categories WHERE recipe_uid=?", (recipe_uid,)
        )
        for c in (LF, cat_uid):
            con.execute(
                "INSERT OR IGNORE INTO recipes_to_categories (recipe_uid, category_uid) "
                "VALUES (?,?)",
                (recipe_uid, c),
            )
        con.commit()
        con.close()
        safe_print(f"Done | Staples folder={cat_uid} recipe={recipe_uid}")


if __name__ == "__main__":
    asyncio.run(main())
