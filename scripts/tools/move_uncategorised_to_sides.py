"""
Move all remaining By type / Uncategorised recipes to Sides & Salads,
then delete the Uncategorised category.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402


def safe_print(*a, **k) -> None:
    try:
        print(*a, **k, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), **k, flush=True)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_recipe(session, headers, recipe: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


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
            raise SystemExit(f"category sync failed: {body[:300]}")


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        uncat = next(c for c in cats if c.get("name") == "Uncategorised")
        sides = next(c for c in cats if c.get("name") == "Sides & Salads")
        uncat_uid = uncat["uid"]
        sides_uid = sides["uid"]
        ul, sl = uncat_uid.lower(), sides_uid.lower()

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        sem = asyncio.Semaphore(6)

        async def load(uid: str) -> dict:
            async with sem:
                for attempt in range(6):
                    async with s.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        return (await resp.json(content_type=None)).get("result") or {}
            return {}

        to_move: list[dict] = []
        for start in range(0, len(index), 50):
            batch = await asyncio.gather(
                *(load(e["uid"]) for e in index[start : start + 50])
            )
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                cats_now = [str(c) for c in (rec.get("categories") or [])]
                if ul not in {c.lower() for c in cats_now}:
                    continue
                to_move.append(rec)

        safe_print(f"Uncategorised recipes to move: {len(to_move)}")
        for rec in sorted(to_move, key=lambda r: (r.get("name") or "").lower()):
            safe_print(f"  - {rec.get('name')}")

        moved = failed = 0
        for rec in to_move:
            cats_now = [str(c) for c in (rec.get("categories") or [])]
            new_cats: list[str] = []
            seen: set[str] = set()
            for c in cats_now:
                repl = sides_uid if c.lower() == ul else c
                if repl.lower() in seen:
                    continue
                seen.add(repl.lower())
                new_cats.append(repl)
            if sl not in seen:
                new_cats.append(sides_uid)
            rec["categories"] = new_cats
            rec["hash"] = calc_hash(rec)
            if await post_recipe(s, headers, rec):
                moved += 1
                safe_print(f"MOVED -> Sides & Salads: {rec.get('name')}")
            else:
                failed += 1
                safe_print(f"FAIL: {rec.get('name')}")
            await asyncio.sleep(0.15)

        # Delete Uncategorised category
        uncat["deleted"] = True
        await post_categories(s, headers, [uncat])
        safe_print("Deleted category: Uncategorised")

        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        safe_print(f"\nDone. moved={moved} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
