"""Restore Field Doctor-Style recipe ingredients/servings from 2026-09-13 backup."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import (  # noqa: E402
    RateLimiter,
    api_json,
    safe_print,
    save_recipe,
)

BAK = ROOT / "backups" / "paprika-json" / "2026-09-13" / "recipes"
APPLY = "--apply" in sys.argv


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def main() -> None:
    files = sorted(BAK.glob("Field_Doctor-Style_*.json"))
    safe_print(f"Backup Field Doctor-Style recipes: {len(files)} MODE={'APPLY' if APPLY else 'DRY'}")
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.45)
    restored = 0
    skipped = 0
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s, limiter, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw}
        )
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for p in files:
            bak = json.loads(p.read_text(encoding="utf-8"))
            uid = bak["uid"]
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
            )
            live = (body or {}).get("result") or {}
            if not live:
                safe_print(f"MISSING {bak.get('name')}")
                continue
            bi = (bak.get("ingredients") or "").strip()
            li = (live.get("ingredients") or "").strip()
            bs = str(bak.get("servings") or "")
            ls = str(live.get("servings") or "")
            if bi == li and bs == ls:
                skipped += 1
                continue
            safe_print(f"RESTORE {live.get('name')}")
            # show a couple diffs
            for a, b in zip(bi.splitlines(), li.splitlines()):
                if a != b:
                    safe_print(f"  {b!r} <- {a!r}")
            if not APPLY:
                restored += 1
                continue
            live["ingredients"] = bak.get("ingredients") or live["ingredients"]
            live["servings"] = bak.get("servings") or live.get("servings")
            # keep notes/description from backup if live lost scale notes — only if backup richer
            if (bak.get("notes") or "") and len(bak.get("notes") or "") > len(live.get("notes") or ""):
                live["notes"] = bak["notes"]
            live["hash"] = calc_hash(live)
            if await save_recipe(s, limiter, headers, live):
                restored += 1
            else:
                safe_print(f"FAIL {live.get('name')}")

        if APPLY and restored:
            await limiter.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
                await r.text()

    safe_print(f"Done. restored={restored} identical={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
