"""
Find and fix recipes needing:
  - Ingredients: stripped from description
  - blank lines between numbered direction steps
Uses exponential backoff on 429.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from cleanup_desc_and_directions import (  # noqa: E402
    RateLimiter,
    api_json,
    calc_hash,
    clean_description,
    clean_directions,
    gzip_obj,
    save_recipe,
)

APPLY = "--apply" in sys.argv
PROGRESS = ROOT / "scripts" / "tools" / ".cleanup_fix_progress.json"


def safe_print(*a, **k) -> None:
    try:
        print(*a, **k, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), **k, flush=True)


async def main() -> None:
    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
        safe_print(f"Resume: {len(done)} saved already")
    elif PROGRESS.exists():
        PROGRESS.unlink()

    user, password = paprika_credentials()
    limiter = RateLimiter(min_interval=0.75, max_backoff=180.0)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        safe_print("Login…")
        status, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        )
        if status != 200:
            raise SystemExit(f"login failed {status} {body}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        status, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers
        )
        index = body["result"]
        safe_print(f"Index {len(index)} MODE={'APPLY' if APPLY else 'SCAN'}")

        # Pass 1: collect dirty UIDs (and optionally fix immediately)
        dirty: list[tuple[str, str, bool, bool]] = []
        saved = failed = scanned = 0
        t0 = time.monotonic()

        for i, entry in enumerate(index, 1):
            uid = entry["uid"]
            if uid in done:
                continue
            status, body = await api_json(
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            if status != 200 or not isinstance(body, dict):
                failed += 1
                continue
            rec = body.get("result")
            if not rec:
                continue
            # Include trash — many dirty copies still live there
            scanned += 1
            name = rec.get("name") or ""
            trash_tag = " [trash]" if rec.get("in_trash") else ""
            desc = rec.get("description")
            dirs = rec.get("directions")
            if desc is not None and not isinstance(desc, str):
                desc = str(desc)
            if dirs is not None and not isinstance(dirs, str):
                dirs = str(dirs)
            desc = desc or ""
            dirs = dirs or ""

            nd = clean_description(desc)
            nr = clean_directions(dirs)
            if nd is None and nr is None:
                if i % 100 == 0:
                    safe_print(
                        f"… {i}/{len(index)} scanned={scanned} dirty={len(dirty)} "
                        f"saved={saved} 429s={limiter.hits_429}"
                    )
                continue

            dirty.append((uid, name, nd is not None, nr is not None))
            safe_print(
                f"DIRTY [{len(dirty)}] {name}{trash_tag} "
                f"(desc={nd is not None}, dirs={nr is not None})"
            )

            if APPLY:
                if nd is not None:
                    rec["description"] = nd
                if nr is not None:
                    rec["directions"] = nr
                rec["hash"] = calc_hash(rec)
                if await save_recipe(s, limiter, headers, rec):
                    saved += 1
                    done.add(uid)
                    safe_print(f"  → SAVED [{saved}]")
                    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
                else:
                    failed += 1
                    safe_print("  → FAIL")
            else:
                done.add(uid)  # scanned

            if i % 50 == 0:
                safe_print(
                    f"… {i}/{len(index)} dirty={len(dirty)} saved={saved} "
                    f"fail={failed} 429s={limiter.hits_429} "
                    f"{(time.monotonic() - t0):.0f}s"
                )

        if APPLY and saved:
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

        safe_print(
            f"\nDone. dirty_found={len(dirty)} saved={saved} failed={failed} "
            f"scanned={scanned} 429s={limiter.hits_429}"
        )
        if APPLY and failed == 0 and dirty:
            PROGRESS.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
