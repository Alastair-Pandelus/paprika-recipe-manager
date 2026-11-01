"""Create the Bay's Kitchen sauce recipes in Paprika (sync API), with jar photos.

Adds the category Low Fodmap > Websites > Bay's Kitchen if missing, and files each
recipe there and under Low Fodmap > Type > Oils, Sauces, Dips and Butters. Skips a
sauce that already exists by name. Dry run unless --apply.

  python scripts/tools/create_bays_kitchen.py [--apply]
"""
from __future__ import annotations

import asyncio
import datetime
import gzip
import hashlib
import io
import json
import sys
import uuid
from pathlib import Path

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from bays_kitchen_sauces import DATA, SAUCES, build  # noqa: E402
from cleanup_library import calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402

IMAGES = ROOT / "data" / "bays_kitchen" / "images"
TYPE_FOLDER = "Oils, Sauces, Dips and Butters"


def gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    apply = "--apply" in sys.argv
    products = json.loads(DATA.read_text(encoding="utf-8"))
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}

        st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=h)
        cats = [x for x in b["result"] if not x.get("deleted")]
        low = next(x for x in cats if x["name"] == "Low Fodmap" and not x.get("parent_uid"))
        websites = next(x for x in cats if x["name"] == "Websites" and x.get("parent_uid") == low["uid"])
        typ = next(x for x in cats if x["name"] == "Type" and x.get("parent_uid") == low["uid"])
        sauces_cat = next(x for x in cats if x["name"] == TYPE_FOLDER and x.get("parent_uid") == typ["uid"])
        bays = next((x for x in cats if x["name"] == "Bay's Kitchen" and x.get("parent_uid") == websites["uid"]), None)
        if not bays:
            sib = [x for x in cats if x.get("parent_uid") == websites["uid"]]
            bays = {"uid": str(uuid.uuid4()).upper(), "name": "Bay's Kitchen", "parent_uid": websites["uid"],
                    "order_flag": max([x.get("order_flag") or 0 for x in sib] + [0]) + 1, "deleted": False}
            print("NEW CATEGORY Low Fodmap > Websites > Bay's Kitchen")
            if apply:
                form = aiohttp.FormData(); form.add_field("data", gz([bays]), content_type="application/octet-stream", filename="data")
                await lim.wait_turn()
                async with s.post(f"{PAPRIKA_API}/v2/sync/categories/", headers=h, data=form) as r:
                    assert '"result":true' in (await r.text()).replace(" ", "")

        st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=h)
        existing_uids = [x["uid"] for x in b["result"]]
        names = set()
        # names live in the local DB; a cheap guard is enough here
        import sqlite3
        from cleanup_library import LOCAL_DB
        con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
        names = {strip_marker(n).lower() for (n,) in con.execute("select name from recipes where coalesce(in_trash,0)=0")}

        made = []
        for handle, recipe in SAUCES.items():
            data = build(handle, products[handle], recipe)
            if strip_marker(data["name"]).lower() in names:
                print("SKIP (exists)", data["name"]); continue
            jpeg_path = IMAGES / f"{handle}-jar.jpg"
            jpeg = jpeg_path.read_bytes()
            uid = str(uuid.uuid4()).upper(); photo = f"{str(uuid.uuid4()).upper()}.jpg"
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec = {
                "uid": uid, "name": data["name"], "ingredients": data["ingredients"], "directions": data["directions"],
                "description": data["description"], "notes": data["notes"], "nutritional_info": "",
                "servings": data["servings"], "difficulty": "", "prep_time": "10 mins", "cook_time": "20 mins", "total_time": "30 mins",
                "source": data["source"], "source_url": data["source_url"], "image_url": None,
                "photo": photo, "photo_large": photo, "photo_hash": hashlib.sha256(jpeg).hexdigest().upper(), "photo_url": None,
                "categories": [bays["uid"], sauces_cat["uid"]], "rating": 0, "in_trash": False, "is_pinned": False,
                "on_favorites": False, "on_grocery_list": False, "scale": None, "created": now,
            }
            rec["hash"] = calc_hash(rec)
            print("NEW", rec["name"], "|", data["description"].splitlines()[-1] if data["description"] else "")
            if not apply:
                continue
            form = aiohttp.FormData()
            form.add_field("data", gz(rec), content_type="application/octet-stream", filename="data")
            form.add_field("photo_upload", jpeg, filename=photo, content_type="image/jpeg")
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h, data=form) as r:
                assert '"result":true' in (await r.text()).replace(" ", ""), rec["name"]
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            got = b["result"]
            assert got["ingredients"] == rec["ingredients"] and got.get("photo") == photo, rec["name"]
            made.append([uid, got["name"], datetime.date.today().isoformat(), "4", 1, "new: Bay's Kitchen home sauce"])
        if apply and made:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as r:
                print("notify", r.status)
            import csv
            with open(ROOT / "docs" / "recipe-audit-log.tsv", "a", encoding="utf-8", newline="") as fh:
                csv.writer(fh, delimiter="\t").writerows(made)
        print("created", len(made))


if __name__ == "__main__":
    asyncio.run(main())
