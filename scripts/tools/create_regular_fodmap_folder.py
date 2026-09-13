"""Create Regular (with Fodmaps) category and move shortbread recipe."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

FOLDER_NAME = "Regular (with Fodmaps)"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_json(obj) -> bytes:
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

        regular = next(
            (c for c in cats if (c.get("name") or "") == FOLDER_NAME),
            None,
        )
        if regular:
            folder_uid = regular["uid"]
            print("Folder exists:", folder_uid)
        else:
            folder_uid = str(uuid.uuid4()).upper()
            item = {
                "uid": folder_uid,
                "name": FOLDER_NAME,
                "parent_uid": None,
                "order_flag": 0,
            }
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_json([item]),
                content_type="application/octet-stream",
                filename="data",
            )
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
            ) as r:
                body = await r.text()
                if '"result":true' not in body.replace(" ", ""):
                    raise SystemExit(f"category create failed: {body[:300]}")
            print("Created folder:", folder_uid)

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        recipe = None
        for entry in index:
            async with session.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                rec = (await r.json()).get("result") or {}
            if rec.get("in_trash"):
                continue
            name = (rec.get("name") or "").lower()
            if "shortbread" in name:
                recipe = rec
                print("FOUND", rec.get("name"), rec.get("uid"))
                # prefer exact shortbread if multiple
                if name.strip() == "shortbread" or name.startswith("shortbread"):
                    break

        if not recipe:
            raise SystemExit("Shortbread recipe not found")

        old = list(recipe.get("categories") or [])
        print("old cats:", old)
        recipe["categories"] = [folder_uid]
        recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipe["hash"] = calc_hash(recipe)

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_json(recipe),
            content_type="application/octet-stream",
            filename="data",
        )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
        ) as r:
            body = await r.text()
            if '"result":true' not in body.replace(" ", ""):
                raise SystemExit(f"recipe update failed: {body[:300]}")

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(
            f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers
        ) as r:
            final = (await r.json())["result"]
        print("Moved:", final.get("name"), "->", final.get("categories"))
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
