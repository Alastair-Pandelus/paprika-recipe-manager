"""Fix Soup - Vegetable Broth 1-serve quantities (source recipe is 8 serves).

Source: https://fodmaptracker.com/recipes/low-fodmap-vegetable-broth/
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fodmap_score_lib import reload_foods, transform_recipe  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
UID = "CC3AAC0C-134B-4AEF-838E-B72ED1ED1D20"

# Correct 1-serve (= original 8-serve ÷ 8)
NEW_INGS = """3/8 medium carrots, roughly chopped
1/8 stalk celery, roughly chopped
1/8 medium parsnip (about 9 g), roughly chopped
1/8 large leek, green tops only, rinsed and roughly chopped (about 2 tbsp)
1/2 to 3/4 spring onion green tops, roughly chopped
1/16 corn cob, broken into a few pieces (optional, for sweetness)
3/4 sprigs fresh parsley
1/2 sprigs fresh thyme
1/8 sprig fresh rosemary
1/4 bay leaves
1/8 tsp whole black peppercorns
3/8 tsp olive oil
1 1/2 cup cold filtered water
Salt, to taste (added after straining)
"""


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    apply = "--apply" in sys.argv
    reload_foods()
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    local = dict(
        con.execute(
            "SELECT uid, name, ingredients, description, notes FROM recipes WHERE uid=?",
            (UID,),
        ).fetchone()
    )
    con.close()

    base_name = re.sub(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*", "", local["name"] or "")
    t = transform_recipe(base_name, NEW_INGS, local["description"] or "", local["notes"] or "")
    # Keep a clear note that this is scaled from 8-serve web recipe
    desc_base = (t["description"] or "").split("\n\n")[0].strip()
    if "Scaled to 1 serve" not in desc_base:
        desc_base = (
            desc_base.rstrip(".")
            + ". Scaled to 1 serve from the FODMAP Tracker 8-serve batch recipe."
        )
    # Re-append fodmap summary via transform on updated desc
    t = transform_recipe(base_name, NEW_INGS, desc_base, local["notes"] or "")

    safe_print(f"Was:\n{local['ingredients']}")
    safe_print(f"Now ingredients:\n{t['ingredients']}")
    safe_print(f"Title: {t['name']}")
    safe_print(f"Stacks: {t['stacks']}")
    safe_print(f"Desc:\n{t['description']}")

    if not apply:
        safe_print("Dry-run only. Pass --apply to save.")
        return

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
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=H
        )
        cloud = (body or {}).get("result") or {}
        if not cloud.get("uid"):
            raise SystemExit("cloud missing")

        t2 = transform_recipe(
            re.sub(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*", "", cloud.get("name") or ""),
            NEW_INGS,
            desc_base,
            cloud.get("notes") or "",
        )
        cloud["name"] = t2["name"]
        cloud["ingredients"] = t2["ingredients"]
        cloud["description"] = t2["description"]
        cloud["notes"] = t2["notes"]
        cloud["servings"] = "1"
        cloud["hash"] = calc_hash(cloud)

        await limiter.wait_turn()
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(cloud),
            content_type="application/octet-stream",
            filename="data",
        )
        async with s.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=H, data=form
        ) as r:
            ok = '"result":true' in (await r.text()).replace(" ", "")
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            await r.text()

    if not ok:
        raise SystemExit("save failed")
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "UPDATE recipes SET name=?, ingredients=?, description=?, notes=?, servings=?, status=? WHERE uid=?",
        (
            t2["name"],
            t2["ingredients"],
            t2["description"],
            t2["notes"],
            "1",
            "modified",
            UID,
        ),
    )
    con.commit()
    con.close()
    safe_print(f"Applied. Meal now {t2['meal_emoji']} stacks={t2['stacks']}")


if __name__ == "__main__":
    asyncio.run(main())
