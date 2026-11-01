"""Add BBC Greek-style lemon chicken orzo soup as a Low FODMAP home adaptation.

Source: https://www.bbc.co.uk/food/recipes/greek-style_lemon_36871
Categories: Websites, Soups. Servings = 1 (scaled from original Serves 4).

  python scripts/tools/add_bbc_lemon_chicken_orzo_soup_lf.py
  python scripts/tools/add_bbc_lemon_chicken_orzo_soup_lf.py --apply
"""
from __future__ import annotations

import argparse
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

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

WEBSITES = "83F5B43B-ECAC-4E1A-9A07-557143CF2B49"
SOUPS = "54C16142-CB9D-4943-88F8-B79B501BD868"
SOURCE_URL = "https://www.bbc.co.uk/food/recipes/greek-style_lemon_36871"

NAME = "Greek-style Lemon Chicken Orzo Soup (Low FODMAP)"

INGREDIENTS = """125 g chicken breast
3/4 tsp sweet paprika
salt and freshly ground black pepper
3/4 tsp olive oil, plus extra to serve
1 tsp garlic-infused olive oil
1/2 medium carrot, peeled and chopped
1/2 small leek, green tops only, sliced (discard white bulb)
1/2 tsp dried oregano
1/8 tsp ground turmeric
375 ml low FODMAP chicken stock (onion- and garlic-free)
zest and juice of 1/2 lemon, plus extra lemon to serve
50 g brown rice pasta (orzo shape / GF orzo)
15 g baby spinach
chopped fresh dill (optional), to garnish
pinch dried red chilli flakes (optional), to garnish
1 tsp lactose-free crème fraîche (optional)
"""

DIRECTIONS = """**Prep (bowls):**

**Large bowl — chicken (fry):**
- Cut the chicken breast into bite-size pieces.
- Season with paprika, salt and pepper; keep in the chicken bowl.

**Medium bowl — veg (soften):**
- Peel and chop the carrot.
- Use only the green leek tops. Discard the white bulb. Slice the greens; add to the veg bowl.
- Have the oregano and turmeric ready with the veg bowl (they soften together).

**Medium bowl — pasta (simmer later):**
- Have the brown rice pasta (orzo shape) ready.

**Medium bowl — finish:**
- Have the baby spinach ready.
- Chop optional dill for garnish.

**By the hob (no bowl):** olive oil, garlic-infused oil, chicken stock, lemon zest and juice, chilli flakes, lactose-free crème fraîche, salt, pepper.

**1. Brown chicken:** Heat the olive oil and garlic-infused oil in a large heavy-based saucepan over medium heat. Fry the chicken bowl until lightly golden. Transfer to a plate.

**2. Soften veg:** Tip in the veg bowl. Cook about 5 minutes until the vegetables soften.

**3. Simmer broth:** Pour in the stock and return the chicken. Simmer 12–15 minutes until the chicken is cooked through (juices run clear). Lift the chicken out, shred with two forks, and return it to the pan with the lemon zest and juice.

**4. Optional freeze point:** Cool and freeze the chicken in broth before adding pasta. To reheat a frozen portion, loosen with a splash of stock or water, then continue from the pasta step.

**5. Pasta:** Add the pasta bowl and cook 8–10 minutes until tender.

**6. Finish:** Stir through the finish bowl until the spinach wilts. Season generously. Serve with a drizzle of olive oil and a squeeze of lemon; optional dill, chilli flakes and lactose-free crème fraîche.
"""

DESCRIPTION = (
    "Ingredients: chicken breast, carrot, leek greens, brown rice orzo, lemon, "
    "spinach, garlic-infused oil, LF stock\n\n"
    "Low FODMAP adaptation of BBC Food Greek-style lemon chicken orzo soup "
    "(Briony Blenman). Garlic powder/cloves removed (garlic-infused oil instead); "
    "leek whites discarded (green tops only); wheat orzo swapped for brown rice "
    "pasta; onion/garlic stock replaced with low FODMAP stock; crème fraîche "
    "optional lactose-free only. Scaled to 1 serve from original Serves 4. "
    "Batch-friendly: freeze before adding pasta."
)

NOTES = (
    f"Source: BBC Food — {SOURCE_URL}\n\n"
    "Low FODMAP home adaptation for personal use (not BBC's published recipe).\n"
    "Swaps: no garlic powder/cloves → garlic-infused oil; leek green tops only; "
    "GF/brown rice orzo; onion-garlic-free stock; lactose-free crème fraîche optional.\n"
    "Check Monash app for current serve sizes."
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def mirror_local(recipe: dict) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute("DELETE FROM recipes WHERE uid = ?", (recipe["uid"],))
    con.execute(
        "DELETE FROM recipes_to_categories WHERE recipe_uid = ?", (recipe["uid"],)
    )
    con.execute(
        """
        INSERT INTO recipes (
            uid, status, name, ingredients, directions, description, notes,
            nutritional_info, servings, difficulty, prep_time, cook_time,
            total_time, rating, in_trash, is_pinned, is_synced, on_favorites,
            source, source_url, scale, sync_hash, created
        ) VALUES (
            ?, 'modified', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0,
            ?, ?, ?, ?, ?
        )
        """,
        (
            recipe["uid"],
            recipe["name"],
            recipe["ingredients"],
            recipe["directions"],
            recipe["description"],
            recipe["notes"],
            recipe.get("nutritional_info") or "",
            recipe["servings"],
            recipe.get("difficulty") or "",
            recipe["prep_time"],
            recipe["cook_time"],
            recipe.get("total_time") or "",
            recipe["source"],
            recipe["source_url"],
            recipe.get("scale") or "",
            recipe.get("hash"),
            recipe["created"],
        ),
    )
    for cuid in recipe["categories"]:
        con.execute(
            "INSERT INTO recipes_to_categories (recipe_uid, category_uid) VALUES (?, ?)",
            (recipe["uid"], cuid),
        )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    safe_print(NAME)
    safe_print(SOURCE_URL)
    safe_print("Categories: Websites, Soups | servings=1")
    safe_print("--- ingredients ---")
    safe_print(INGREDIENTS.strip())
    if not args.apply:
        safe_print("Dry-run only. Pass --apply to create in Paprika.")
        return

    uid = str(uuid.uuid4()).upper()
    recipe = {
        "uid": uid,
        "name": NAME,
        "ingredients": INGREDIENTS.strip() + "\n",
        "directions": DIRECTIONS.strip() + "\n",
        "description": DESCRIPTION,
        "notes": NOTES,
        "servings": "1",
        "prep_time": "less than 30 mins",
        "cook_time": "30 mins to 1 hour",
        "total_time": "",
        "source": "BBC Food",
        "source_url": SOURCE_URL,
        "categories": [WEBSITES, SOUPS],
        "rating": 0,
        "difficulty": "",
        "nutrition": None,
        "nutritional_info": "",
        "photo": None,
        "photo_hash": None,
        "photo_large": None,
        "photo_url": None,
        "image_url": None,
        "in_trash": False,
        "is_pinned": False,
        "on_favorites": False,
        "on_grocery_list": False,
        "scale": None,
        "hash": None,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    recipe["hash"] = calc_hash(recipe)

    user, pw = paprika_credentials()
    lim = RateLimiter(0.28)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session,
            lim,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        await lim.wait_turn()
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(recipe),
            content_type="application/octet-stream",
            filename="data",
        )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers, data=form
        ) as r:
            txt = await r.text()
            if '"result":true' not in txt.replace(" ", ""):
                raise SystemExit(f"create failed: {txt[:400]}")
        safe_print(f"Created uid={uid}")

        await lim.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            safe_print("notify", r.status)

    mirror_local(recipe)
    safe_print("Mirrored to local SQLite.")
    safe_print(uid)


if __name__ == "__main__":
    asyncio.run(main())
