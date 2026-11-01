"""Apply the coconut oil swaps (shecanteatwhat_coconut_swaps.py) to the She Can't Eat What
recipes already in Paprika, and import the ones the swap brings under 10 g saturated fat
a serving. An imported recipe is only updated if it is unchanged since the import (else it
is listed for a hand check). Dry run unless --apply.

  python scripts/tools/apply_shecanteatwhat_coconut_swaps.py [--apply]
"""
from __future__ import annotations

import asyncio
import datetime
import gzip
import hashlib
import json
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import import_shecanteatwhat_recipes as imp  # noqa: E402
from cleanup_library import calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402

DATA = ROOT / "data" / "shecanteatwhat"


def description(d: dict) -> str:
    return (d["intro"] + "\n\n" if d["intro"] else "") + (d["description"] or "")


async def main() -> None:
    apply = "--apply" in sys.argv
    data = json.loads((DATA / "recipes.json").read_text(encoding="utf-8"))
    dups = set(json.loads((DATA / "duplicate_uids.json").read_text()))
    # the 82 imported originals: uid by source URL (the desktop app hadn't synced them yet)
    have = {x[2].rstrip("/").lower(): x[0] for x in json.loads((DATA / "server_new.json").read_text(encoding="utf-8"))
            if x[0] not in dups}
    real_swap = imp.coconut_swap
    todo = [r for r in data if any("coconut oil" in l.lower() for l in r["ing"])]
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=h)
        on_server = {x["uid"] for x in b["result"]}

        async def post(url, obj, photo=None, photo_name=None):
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(obj, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            if photo:
                form.add_field("photo_upload", photo, filename=photo_name, content_type="image/jpeg")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as r:
                assert '"result":true' in (await r.text()).replace(" ", ""), url

        done = 0
        for r in todo:
            imp.coconut_swap = lambda x: (x, "")
            before = imp.prepare(r)
            imp.coconut_swap = real_swap
            d = imp.prepare(r)
            uid = have.get(r["url"].rstrip("/").lower())
            if uid:
                assert uid in on_server
                url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
                st, b = await api_json(s, lim, "GET", url, headers=h)
                cur = b["result"]
                if cur["ingredients"] != before["ingredients"] or cur.get("in_trash"):
                    print("SKIP (changed since import)", cur["name"]); continue
                print(f"UPDATE {before['name'][:55]:55} -> {d['name'][:8]} sat {before['sat']} -> {d['sat']} g")
                if not apply:
                    continue
                with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({k: cur.get(k) for k in ("uid", "name", "ingredients", "directions", "notes", "description")},
                                        ensure_ascii=False) + "\n")
                upd = dict(cur, name=d["name"], ingredients=d["ingredients"], directions=d["directions"], notes=d["notes"],
                           description=description(d))
                upd["hash"] = calc_hash(upd)
                await post(url, upd)
                st, b = await api_json(s, lim, "GET", url, headers=h)
                assert b["result"]["ingredients"] == upd["ingredients"], d["name"]
                done += 1
            elif d["servings"] and d["fod"] != "❌" and d["chol"] != "💔":
                print(f"NEW    {d['name'][:55]:55} sat {before['sat']} -> {d['sat']} g ({d['type']})")
                if not apply:
                    continue
                st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=h)
                cats = [x for x in b["result"] if not x.get("deleted")]

                def child(parent, name):
                    return next(x for x in cats if x["name"] == name and x.get("parent_uid") == parent["uid"])
                low = next(x for x in cats if x["name"] == "Low Fodmap" and not x.get("parent_uid"))
                folder, typ = child(child(low, "Websites"), imp.FOLDER), child(low, "Type")
                vegan = child(child(low, "Diet"), "Vegan")
                img = imp.jpeg(d["img"])
                uid = str(uuid.uuid4()).upper(); photo = f"{str(uuid.uuid4()).upper()}.jpg" if img else None
                rec = {
                    "uid": uid, "name": d["name"], "ingredients": d["ingredients"], "directions": d["directions"],
                    "description": description(d), "notes": d["notes"], "nutritional_info": "", "servings": d["servings"],
                    "difficulty": "", "prep_time": "", "cook_time": "", "total_time": d["total"], "source": imp.SOURCE,
                    "source_url": d["url"], "image_url": None, "photo": photo, "photo_large": photo,
                    "photo_hash": hashlib.sha256(img).hexdigest().upper() if img else None, "photo_url": None,
                    "categories": [folder["uid"], child(typ, d["type"])["uid"]] + ([vegan["uid"]] if d["vegan"] else []),
                    "rating": 0, "in_trash": False, "is_pinned": False, "on_favorites": False, "on_grocery_list": False,
                    "scale": None, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                rec["hash"] = calc_hash(rec)
                await post(f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", rec, img, photo)
                st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
                assert b["result"]["ingredients"] == rec["ingredients"], d["name"]
                with open(DATA / "server_new.json", encoding="utf-8") as fh:
                    known = json.load(fh)
                known.append([uid, d["name"], d["url"], rec["created"], False])
                (DATA / "server_new.json").write_text(json.dumps(known, ensure_ascii=False, indent=0), encoding="utf-8")
                done += 1
            else:
                print(f"STILL OUT {d['name'][:55]} (sat {d['sat']} g, servings {d['servings'] or 'none'})")
        if apply and done:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as r:
                print("notify", r.status)
        print("written", done)


if __name__ == "__main__":
    asyncio.run(main())
