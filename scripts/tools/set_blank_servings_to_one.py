"""Set blank servings to 1 on active Paprika recipes (local + cloud)."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
APPLY = "--apply" in sys.argv


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def list_blank() -> list[tuple[str, str]]:
    con = sqlite3.connect(LOCAL_DB)
    rows = con.execute(
        """
        SELECT uid, name FROM recipes
        WHERE coalesce(in_trash, 0) = 0
          AND (servings IS NULL OR trim(servings) = '')
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    return [(u, n or "") for u, n in rows]


async def main() -> None:
    blanks = list_blank()
    safe_print(f"Blank servings: {len(blanks)} | MODE={'APPLY' if APPLY else 'DRY'}")
    for uid, name in blanks:
        safe_print(f"  -> 1 | {name}")

    if not APPLY:
        safe_print("Dry-run only. Pass --apply to write.")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.3)
    ok_n = fail_n = 0
    con = sqlite3.connect(LOCAL_DB)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}
        for uid, name in blanks:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                fail_n += 1
                safe_print(f"FAIL missing {name}")
                continue
            rec["servings"] = "1"
            rec["hash"] = calc_hash(rec)
            await limiter.wait_turn()
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H, data=form
            ) as r:
                body = await r.text()
                ok = '"result":true' in body.replace(" ", "")
            if ok:
                ok_n += 1
                con.execute(
                    "UPDATE recipes SET servings=?, status=? WHERE uid=?",
                    ("1", "modified", uid),
                )
            else:
                fail_n += 1
                safe_print(f"FAIL save {name}")
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
    con.commit()
    con.close()
    safe_print(f"done ok={ok_n} fail={fail_n}")


if __name__ == "__main__":
    asyncio.run(main())
