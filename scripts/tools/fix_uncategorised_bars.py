"""
Move clearly bar / bar-style snacks out of By type / Uncategorised
into By type / Snacks & Bars.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

# Titles that clearly belong in Snacks & Bars
BAR_TITLE = re.compile(
    r"\b("
    r"snack\s*bars?|krispie\s*bars?|pb\s*&\s*j\s*bars?|granola\s*bars?|"
    r"protein\s*bars?|energy\s*bars?|s'?mores\s*bars?|"
    r"rice\s*krispie|chocolate\s*peanut\s*butter\s*balls|"
    r"energy\s*balls|mac\s*nut\s*clusters|coconut\s*snowballs"
    r")\b",
    re.I,
)


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


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        uncat = next(c for c in cats if c.get("name") == "Uncategorised")
        snacks = next(c for c in cats if c.get("name") == "Snacks & Bars")
        uncat_uid = uncat["uid"]
        snacks_uid = snacks["uid"]
        ul, sl = uncat_uid.lower(), snacks_uid.lower()

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        sem = asyncio.Semaphore(5)
        moved = 0

        async def load(uid: str) -> dict:
            async with sem:
                for attempt in range(6):
                    async with s.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as r:
                        if r.status == 429:
                            await asyncio.sleep(1.3 * (attempt + 1))
                            continue
                        return (await r.json(content_type=None)).get("result") or {}
            return {}

        for start in range(0, len(index), 40):
            batch = await asyncio.gather(*(load(e["uid"]) for e in index[start : start + 40]))
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                cats_now = [str(c) for c in (rec.get("categories") or [])]
                if ul not in {c.lower() for c in cats_now}:
                    continue
                name = rec.get("name") or ""
                if not BAR_TITLE.search(name):
                    continue
                # swap Uncategorised -> Snacks & Bars
                new_cats = []
                seen = set()
                for c in cats_now:
                    repl = snacks_uid if c.lower() == ul else c
                    if repl.lower() in seen:
                        continue
                    seen.add(repl.lower())
                    new_cats.append(repl)
                if sl not in seen:
                    new_cats.append(snacks_uid)
                rec["categories"] = new_cats
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(s, headers, rec)
                if ok:
                    moved += 1
                    safe_print(f"MOVED -> Snacks & Bars: {name}")
                else:
                    safe_print(f"FAIL: {name}")
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.3)

        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        safe_print(f"\nDone. moved={moved}")


if __name__ == "__main__":
    asyncio.run(main())
