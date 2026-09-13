"""Trash Field Doctor - Bars recipes + folder; rename Meals to Field Doctor."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

BARS = "1DFE7F9B-D3F0-43C2-A640-62D290352F61"
MEALS = "DFD7AB80-DB1E-4257-A6E9-7E9AA44E548C"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        bars_cat = next((c for c in cats if c["uid"].lower() == BARS.lower()), None)
        meals_cat = next((c for c in cats if c["uid"].lower() == MEALS.lower()), None)
        # also match by name if UIDs drifted
        if not bars_cat:
            bars_cat = next(
                (
                    c
                    for c in cats
                    if (c.get("name") or "").lower() in {"field doctor - bars", "field doctor bars"}
                ),
                None,
            )
        if not meals_cat:
            meals_cat = next(
                (
                    c
                    for c in cats
                    if (c.get("name") or "").lower()
                    in {"field doctor - meals", "field doctor meals"}
                ),
                None,
            )
        print("Bars cat:", bars_cat and bars_cat.get("name"), bars_cat and bars_cat.get("uid"))
        print("Meals cat:", meals_cat and meals_cat.get("name"), meals_cat and meals_cat.get("uid"))

        bars_uid = (bars_cat or {}).get("uid") or BARS
        meals_uid = (meals_cat or {}).get("uid") or MEALS

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        trashed = []
        for entry in index:
            async with session.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                rec = (await r.json()).get("result") or {}
            if rec.get("in_trash"):
                continue
            cats_l = [c.lower() for c in (rec.get("categories") or [])]
            name = rec.get("name") or ""
            is_bar = bars_uid.lower() in cats_l or (
                "field doctor-style" in name.lower() and "bar" in name.lower()
            )
            if not is_bar:
                continue
            print("TRASH", name)
            rec["in_trash"] = True
            rec["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec["hash"] = calc_hash(rec)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{rec['uid']}/", headers=headers, data=form
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            if not ok:
                raise SystemExit(f"Failed to trash {name}")
            trashed.append(name)

        print(f"Trashed {len(trashed)} bar recipes")

        # Delete Bars folder
        if bars_cat:
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj([{**bars_cat, "deleted": True}]),
                content_type="application/octet-stream",
                filename="data",
            )
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
            ) as r:
                body = await r.text()
                if '"result":true' not in body.replace(" ", ""):
                    raise SystemExit(f"Bars folder delete failed: {body[:300]}")
            print("Deleted folder: Field Doctor - Bars")
        else:
            print("Bars folder already missing")

        # Rename Meals -> Field Doctor
        if not meals_cat:
            raise SystemExit("Field Doctor - Meals folder not found")
        renamed = {
            **meals_cat,
            "name": "Field Doctor",
            "parent_uid": meals_cat.get("parent_uid") or LF,
        }
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj([renamed]),
            content_type="application/octet-stream",
            filename="data",
        )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
        ) as r:
            body = await r.text()
            if '"result":true' not in body.replace(" ", ""):
                raise SystemExit(f"Rename failed: {body[:300]}")
        print("Renamed Field Doctor - Meals -> Field Doctor")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats2 = [c for c in (await r.json())["result"] if not c.get("deleted")]
        print("\nLow Fodmap tree:")
        for c in cats2:
            if c["uid"].lower() == LF.lower() or (c.get("parent_uid") or "").lower() == LF.lower():
                print(f"  {c.get('name')}  parent={c.get('parent_uid')}")
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
