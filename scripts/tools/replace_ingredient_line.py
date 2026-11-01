"""Replace one ingredient line in one recipe, through the sync API only.

Fetches the recipe from the cloud, swaps the line that matches OLD exactly
(after trimming) for NEW, leaves everything else as it is, then posts it and
notifies sync. Never writes the local SQLite; refuses while that recipe is
pending locally. Dry-run unless --apply.

  python scripts/tools/replace_ingredient_line.py UID "Optional: 50 g dark chocolate chips" "50 g dark chocolate chips (optional)"
"""
from __future__ import annotations

import argparse
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


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def local_pending(uid: str) -> bool:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("select status, is_synced from recipes where uid = ?", (uid,)).fetchone()
    finally:
        con.close()
    return bool(row) and (row[0] != "unmodified" or row[1] == 0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("uid")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    user, pw = paprika_credentials()
    lim = RateLimiter(0.28)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        url = f"{PAPRIKA_API}/v2/sync/recipe/{args.uid}/"
        st, body = await api_json(session, lim, "GET", url, headers=headers)
        recipe = (body or {}).get("result")
        if st != 200 or not recipe:
            raise SystemExit(f"recipe GET failed {st}")

        lines = (recipe.get("ingredients") or "").split("\n")
        hits = [i for i, l in enumerate(lines) if l.strip() == args.old.strip()]
        if len(hits) != 1:
            raise SystemExit(f"expected 1 matching line in {recipe['name']!r}, found {len(hits)}")
        safe_print(f"{recipe['name']}\n  - {lines[hits[0]]}\n  + {args.new}")
        lines[hits[0]] = args.new

        if local_pending(args.uid):
            safe_print("Local SQLite has this recipe pending; the desktop app would overwrite it.")
            if args.apply:
                raise SystemExit("not applied")
        if not args.apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        new = dict(recipe, ingredients="\n".join(lines))
        new["hash"] = calc_hash(new)
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip.compress(json.dumps(new, separators=(",", ":")).encode()),
            content_type="application/octet-stream",
            filename="data",
        )
        await lim.wait_turn()
        async with session.post(url, headers=headers, data=form) as r:
            txt = await r.text()
            if '"result":true' not in txt.replace(" ", ""):
                raise SystemExit(f"recipe POST failed: {r.status} {txt[:400]}")
        await lim.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            safe_print("Updated; notify", r.status)

        st, body = await api_json(session, lim, "GET", url, headers=headers)
        safe_print("--- ingredients now ---\n" + ((body or {}).get("result") or {}).get("ingredients", ""))


if __name__ == "__main__":
    asyncio.run(main())
