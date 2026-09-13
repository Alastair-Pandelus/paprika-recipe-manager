"""Reorganise Low Fodmap: Field Doctor - Meals/Bars + Monash subfolders."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp

ENV = Path.home() / ".paprika-mcp.env"
BASE = "https://paprikaapp.com/api"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
MEALS_OLD = "DFD7AB80-DB1E-4257-A6E9-7E9AA44E548C"
BARS_OLD = "1DFE7F9B-D3F0-43C2-A640-62D290352F61"

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_categories(session, headers, items: list[dict]) -> None:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_json(items), content_type="application/octet-stream", filename="data"
    )
    async with session.post(f"{BASE}/v2/sync/categories/", headers=headers, data=form) as r:
        body = await r.text()
        if '"result":true' not in body.replace(" ", ""):
            raise SystemExit(f"categories POST failed: {body[:300]}")


async def post_recipe(session, headers, recipe: dict) -> bool:
    recipe = dict(recipe)
    recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recipe["hash"] = calc_hash(recipe)
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_json(recipe), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        return '"result":true' in (await r.text()).replace(" ", "")


def is_monash(rec: dict) -> bool:
    url = (rec.get("source_url") or "").lower()
    src = (rec.get("source") or "").lower()
    return "monashfodmap.com" in url or "monashfodmap.com" in src


def norm_cats(cats: list) -> list[str]:
    return list(cats or [])


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{BASE}/v2/sync/categories/", headers=headers) as r:
            cats = (await r.json())["result"]
        by_uid = {c["uid"]: c for c in cats}

        meals_uid = MEALS_OLD if MEALS_OLD in by_uid else str(uuid.uuid4()).upper()
        bars_uid = BARS_OLD if BARS_OLD in by_uid else str(uuid.uuid4()).upper()
        monash_uid = str(uuid.uuid4()).upper()
        # reuse Monash if already exists under LF
        for c in cats:
            if (c.get("name") or "").lower() == "monash" and (
                c.get("parent_uid") or ""
            ).lower() == LF.lower():
                monash_uid = c["uid"]

        cat_updates = []
        # Field Doctor Meals under Low Fodmap
        meals_cat = dict(by_uid.get(meals_uid) or {"uid": meals_uid})
        meals_cat.update(
            {"uid": meals_uid, "name": "Field Doctor - Meals", "parent_uid": LF, "order_flag": 0}
        )
        cat_updates.append(meals_cat)

        bars_cat = dict(by_uid.get(bars_uid) or {"uid": bars_uid})
        bars_cat.update(
            {
                "uid": bars_uid,
                "name": "Field Doctor - Bars",
                "parent_uid": LF,
                "order_flag": 1,
            }
        )
        cat_updates.append(bars_cat)

        monash_cat = dict(by_uid.get(monash_uid) or {"uid": monash_uid})
        monash_cat.update(
            {"uid": monash_uid, "name": "Monash", "parent_uid": LF, "order_flag": 2}
        )
        cat_updates.append(monash_cat)

        # Soft-delete empty top-level Field Doctor parent (no longer needed)
        if FD in by_uid:
            fd_cat = dict(by_uid[FD])
            fd_cat["deleted"] = True
            cat_updates.append(fd_cat)

        await post_categories(session, headers, cat_updates)
        print("Categories updated:")
        print(f"  Field Doctor Meals  {meals_uid}  parent=Low Fodmap")
        print(f"  Field Doctor - Bars {bars_uid}  parent=Low Fodmap")
        print(f"  Monash              {monash_uid}  parent=Low Fodmap")
        print(f"  Field Doctor root soft-deleted")

        # Reassign recipes
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        remove_uids = {FD.lower(), MEALS_OLD.lower(), BARS_OLD.lower(), LF.lower()}
        # also remove any previous monash if reassigning
        n_meals = n_bars = n_monash = 0
        fails = 0

        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                rec = (await r.json()).get("result") or {}
            if rec.get("in_trash"):
                continue
            name = rec.get("name") or ""
            cats = norm_cats(rec.get("categories"))
            cats_l = [c.lower() for c in cats]
            changed = False
            target = None

            if "Field Doctor-Style" in name and "Bar" in name:
                target = bars_uid
                n_bars += 1
            elif "Field Doctor-Style" in name:
                target = meals_uid
                n_meals += 1
            elif is_monash(rec):
                target = monash_uid
                n_monash += 1

            if target:
                # strip old LF/FD structure uids, keep unrelated categories
                new_cats = [
                    c
                    for c in cats
                    if c.lower() not in remove_uids
                    and c.lower() != monash_uid.lower()
                    and c.lower() != meals_uid.lower()
                    and c.lower() != bars_uid.lower()
                ]
                new_cats.append(target)
                if new_cats != cats:
                    rec["categories"] = new_cats
                    changed = True
            elif LF.lower() in cats_l or FD.lower() in cats_l or MEALS_OLD.lower() in cats_l or BARS_OLD.lower() in cats_l:
                # LF-tagged non-FD non-Monash: keep under Low Fodmap root only
                new_cats = [
                    c
                    for c in cats
                    if c.lower()
                    not in {
                        FD.lower(),
                        MEALS_OLD.lower(),
                        BARS_OLD.lower(),
                        meals_uid.lower(),
                        bars_uid.lower(),
                        monash_uid.lower(),
                    }
                ]
                if LF not in new_cats and LF.lower() not in [x.lower() for x in new_cats]:
                    new_cats.append(LF)
                if new_cats != cats:
                    rec["categories"] = new_cats
                    changed = True

            if changed:
                ok = await post_recipe(session, headers, rec)
                if not ok:
                    fails += 1
                    print("FAIL", name)

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        print(
            f"Assigned: meals={n_meals}, bars={n_bars}, monash={n_monash}, fails={fails}"
        )
        async with session.get(f"{BASE}/v2/sync/categories/", headers=headers) as r:
            cats = (await r.json())["result"]
        print("\nLow Fodmap tree:")
        for c in cats:
            if (c.get("uid") or "").lower() == LF.lower() or (
                c.get("parent_uid") or ""
            ).lower() == LF.lower():
                print(
                    f"  {c.get('name')}  parent={c.get('parent_uid')}  deleted={c.get('deleted')}"
                )


asyncio.run(main())
