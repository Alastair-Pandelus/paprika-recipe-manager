"""Replace the sauce text in the Bay's Kitchen recipes with Paprika recipe links
([recipe:<exact sauce name>]) in the ingredients, the first method step and the notes.
Dry run unless --apply.

  python scripts/tools/link_bays_kitchen_sauces.py [--apply]
"""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from bays_kitchen_sauces import SAUCES  # noqa: E402
from cleanup_library import LOCAL_DB, calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402


def gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    live = {strip_marker(n): (u, n) for u, n in con.execute("select uid, name from recipes where coalesce(in_trash,0)=0")}
    full = {r["name"]: live[r["name"]][1] for r in SAUCES.values()}  # plain sauce name -> exact current name
    recipes = [(u, n) for u, n in con.execute("select uid, name from recipes where coalesce(in_trash,0)=0 and name like '%Bay''s Kitchen % – %'")]
    print(len(recipes), "recipes")
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    done = 0
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        for uid, name in recipes:
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, b = await api_json(s, lim, "GET", url, headers=h); r = b["result"]
            sauce = next((p for p in full if p in r["ingredients"] or p in (r.get("notes") or "")), None)
            if not sauce:
                print("?? no sauce text", name); continue
            link = f"[recipe:{full[sauce]}]"
            ing = re.sub(re.escape(sauce) + r" \(home-made, see that recipe\)", link, r["ingredients"])
            directions = r["directions"].replace(f"Make the home-made {sauce} first (see that recipe), or use a frozen portion.",
                                                 f"Make the home-made sauce first, {link}, or use a frozen portion.")
            notes = (r.get("notes") or "").replace(f"Uses the home-made {sauce} instead", f"Uses the home-made {link} instead")
            if ing == r["ingredients"]:
                print("== unchanged", name); continue
            t = f.transform_recipe(r["name"], ing, r.get("description") or "", notes, servings=r.get("servings"))
            t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=r.get("servings"))
            if t2["name"] != r["name"]:
                print("   marker", r["name"], "->", t2["name"])
            done += 1
            if not apply:
                if done <= 2:
                    print([l for l in t2["ingredients"].split("\n") if "[recipe:" in l], directions.split("\n")[0])
                continue
            upd = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], directions=directions, notes=notes)
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData(); form.add_field("data", gz(upd), content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), name
            st, b = await api_json(s, lim, "GET", url, headers=h)
            assert b["result"]["ingredients"] == upd["ingredients"], name
        if apply and done:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
                print("notify", resp.status)
    print("linked", done)


if __name__ == "__main__":
    asyncio.run(main())
