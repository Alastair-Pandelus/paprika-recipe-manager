"""Create the adapted Bay's Kitchen recipes in Paprika (sync API), with their photos.

Folders: Low Fodmap > Websites > Bay's Kitchen > <sauce> (created if missing); each recipe
goes in its sauce's sub-folder and its Low Fodmap > Type folder (and Diet > Vegan when
plant-based). The home-made sauce recipes are also added to their sub-folder. Skips a
recipe whose name already exists. Dry run unless --apply.

  python scripts/tools/create_bays_kitchen_recipes.py [--apply]
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import gzip
import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from bays_kitchen_sauces import SAUCES  # noqa: E402
from cleanup_library import LOCAL_DB, calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402

DRAFTS = ROOT / "data" / "bays_kitchen" / "recipe_drafts.json"
IMAGES = ROOT / "data" / "bays_kitchen" / "recipe_images"


def gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post(s, lim, h, url, obj, photo=None, photo_name=None):
    form = aiohttp.FormData()
    form.add_field("data", gz(obj), content_type="application/octet-stream", filename="data")
    if photo:
        form.add_field("photo_upload", photo, filename=photo_name, content_type="image/jpeg")
    await lim.wait_turn()
    async with s.post(url, headers=h, data=form) as r:
        assert '"result":true' in (await r.text()).replace(" ", ""), url


async def main() -> None:
    apply = "--apply" in sys.argv
    drafts = json.loads(DRAFTS.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    names = {strip_marker(n).lower(): u for u, n in con.execute("select uid, name from recipes where coalesce(in_trash,0)=0")}
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    log = []
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=h)
        cats = [x for x in b["result"] if not x.get("deleted")]

        def child(parent, name):
            return next((x for x in cats if x["name"] == name and x.get("parent_uid") == parent["uid"]), None)
        low = next(x for x in cats if x["name"] == "Low Fodmap" and not x.get("parent_uid"))
        bays = child(child(low, "Websites"), "Bay's Kitchen")
        typ, diet = child(low, "Type"), child(low, "Diet")
        vegan = child(diet, "Vegan")

        sub: dict[str, dict] = {}
        new_cats = []
        for i, name in enumerate(sorted({d["subfolder"] for d in drafts})):
            c = child(bays, name)
            if not c:
                c = {"uid": str(uuid.uuid4()).upper(), "name": name, "parent_uid": bays["uid"], "order_flag": i, "deleted": False}
                new_cats.append(c); cats.append(c)
            sub[name] = c
        print(f"sub-folders: {len(sub)} ({len(new_cats)} new)")
        if apply and new_cats:
            await post(s, lim, h, f"{PAPRIKA_API}/v2/sync/categories/", new_cats)

        # the home-made sauces into their sub-folders
        from adapt_bays_kitchen_recipes import SUBFOLDER
        for handle, r in SAUCES.items():
            uid = names.get(strip_marker(r["name"]).lower())
            if not uid:
                print("sauce not found:", r["name"]); continue
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            rec = b["result"]; target = sub[SUBFOLDER[handle]]["uid"]
            if target in rec["categories"]:
                continue
            print("sauce -> sub-folder:", rec["name"], "->", SUBFOLDER[handle])
            if apply:
                upd = dict(rec, categories=rec["categories"] + [target]); upd["hash"] = calc_hash(upd)
                await post(s, lim, h, f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", upd)

        for d in drafts:
            if strip_marker(d["name"]).lower() in names:
                print("SKIP (exists)", d["name"]); continue
            cats_for = [sub[d["subfolder"]]["uid"], child(typ, d["type"])["uid"]] + ([vegan["uid"]] if d.get("vegan") and vegan else [])
            img = IMAGES / f"{d['handle']}.jpg"
            jpeg = img.read_bytes() if img.exists() else None
            uid = str(uuid.uuid4()).upper(); photo = f"{str(uuid.uuid4()).upper()}.jpg" if jpeg else None
            prep = f"{d['prep']} mins" if d.get("prep") else ""
            cook = f"{d['cook']} mins" if d.get("cook") else ""
            rec = {
                "uid": uid, "name": d["name"], "ingredients": d["ingredients"], "directions": d["directions"],
                "description": d["description"], "notes": d["notes"], "nutritional_info": "", "servings": str(d["servings"]),
                "difficulty": "", "prep_time": prep, "cook_time": cook, "total_time": "", "source": "Bay's Kitchen",
                "source_url": d["url"], "image_url": None, "photo": photo, "photo_large": photo,
                "photo_hash": hashlib.sha256(jpeg).hexdigest().upper() if jpeg else None, "photo_url": None,
                "categories": cats_for, "rating": 0, "in_trash": False, "is_pinned": False, "on_favorites": False,
                "on_grocery_list": False, "scale": None, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            rec["hash"] = calc_hash(rec)
            print(f"NEW {d['subfolder']:16} | {d['type']:13} | {'vegan ' if d.get('vegan') else '      '}| {'photo' if jpeg else 'NO PHOTO'} | {d['name']}")
            if not apply:
                continue
            await post(s, lim, h, f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", rec, jpeg, photo)
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            assert b["result"]["ingredients"] == rec["ingredients"], d["name"]
            log.append([uid, b["result"]["name"], datetime.date.today().isoformat(), "4", 1, "new: Bay's Kitchen recipe (adapted)"])
        if apply:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as r:
                print("notify", r.status)
            with open(ROOT / "docs" / "recipe-audit-log.tsv", "a", encoding="utf-8", newline="") as fh:
                csv.writer(fh, delimiter="\t").writerows(log)
        print("created", len(log))


if __name__ == "__main__":
    asyncio.run(main())
