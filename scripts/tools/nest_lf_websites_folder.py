"""
Nest website source folders under Low Fodmap / Websites.
Leaves Type and Diet as direct children of Low Fodmap.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
WEBSITES = "Websites"
KEEP_AS_LF_CHILDREN = {WEBSITES.lower(), "by type", "diet"}


def safe_print(*a, **k) -> None:
    try:
        print(*a, **k, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), **k, flush=True)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_categories(session, headers, items: list[dict]) -> None:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(items), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
    ) as r:
        body = await r.text()
        if '"result":true' not in body.replace(" ", ""):
            raise SystemExit(f"category sync failed: {body[:400]}")


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]

        websites = next(
            (
                c
                for c in cats
                if (c.get("name") or "") == WEBSITES
                and (c.get("parent_uid") or "").lower() == LF.lower()
            ),
            None,
        )
        if websites:
            websites_uid = websites["uid"]
            safe_print("Websites folder exists:", websites_uid)
        else:
            websites_uid = str(uuid.uuid4()).upper()
            item = {
                "uid": websites_uid,
                "name": WEBSITES,
                "parent_uid": LF,
                "order_flag": 0,
            }
            await post_categories(s, headers, [item])
            cats.append(item)
            safe_print("Created Websites:", websites_uid)

        to_move = [
            c
            for c in cats
            if (c.get("parent_uid") or "").lower() == LF.lower()
            and (c.get("name") or "").lower() not in KEEP_AS_LF_CHILDREN
        ]
        safe_print(f"Folders to nest under Websites: {len(to_move)}")
        updates = []
        for i, c in enumerate(sorted(to_move, key=lambda x: (x.get("name") or "").lower())):
            c["parent_uid"] = websites_uid
            c["order_flag"] = i
            updates.append(
                {
                    "uid": c["uid"],
                    "name": c["name"],
                    "parent_uid": websites_uid,
                    "order_flag": i,
                }
            )
            safe_print(f"  -> Websites / {c['name']}")

        if updates:
            # Paprika category sync accepts batches
            await post_categories(s, headers, updates)

        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        # Verify
        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats2 = [c for c in (await r.json())["result"] if not c.get("deleted")]
        safe_print("\nLow Fodmap children:")
        for c in sorted(cats2, key=lambda x: (x.get("name") or "").lower()):
            if (c.get("parent_uid") or "").lower() == LF.lower():
                safe_print(f"  {c.get('name')}")
        safe_print("\nWebsites children:")
        for c in sorted(cats2, key=lambda x: (x.get("name") or "").lower()):
            if (c.get("parent_uid") or "").lower() == websites_uid.lower():
                safe_print(f"  {c.get('name')}")


if __name__ == "__main__":
    asyncio.run(main())
