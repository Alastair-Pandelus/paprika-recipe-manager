"""Add every pantry staple to the main Paprika grocery list (planning step 2).

Reads the cloud pantry, takes each in-stock, non-deleted ``Staple - <name>``
row, and adds one grocery item per staple: named without the prefix, with the
pantry quantity and aisle (e.g. "Eggs", 12, Dairy). A staple already on the
list as an unpurchased item is skipped, so re-running never duplicates.

Never edits or deletes other groceries, never touches the pantry, writes
through the sync API only and refuses while the local SQLite has pending
grocery rows. Dry-run unless --apply.

  python scripts/tools/add_staples_to_groceries.py
  python scripts/tools/add_staples_to_groceries.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import sqlite3
import sys
import uuid
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
STAPLE_PREFIX = "Staple - "


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def name_key(text: str) -> str:
    """Compare grocery names ignoring a leading quantity, case and punctuation."""
    s = (text or "").lower()
    s = re.sub(r"^\s*\d+(?:[./]\d+)?\s*(?:x\s+)?", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def local_pending_groceries() -> int:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            """
            select count(*) from grocery_items
            where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0
            """
        ).fetchone()[0]
    finally:
        con.close()


def pantry_staples(pantry: list) -> list[dict]:
    return [
        x
        for x in pantry
        if (x.get("ingredient") or "").startswith(STAPLE_PREFIX)
        and x.get("in_stock")
        and not x.get("deleted")
    ]


async def get_result(session, lim, headers, endpoint: str) -> list:
    st, body = await api_json(session, lim, "GET", f"{PAPRIKA_API}{endpoint}", headers=headers)
    if st != 200:
        raise SystemExit(f"GET {endpoint} failed {st}")
    return (body or {}).get("result") or []


async def post_entities(session, limiter, headers, endpoint: str, items: list) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(items),
        content_type="application/octet-stream",
        filename="data",
    )
    await limiter.wait_turn()
    async with session.post(f"{PAPRIKA_API}{endpoint}", headers=headers, data=form) as r:
        txt = await r.text()
        ok = '"result":true' in txt.replace(" ", "")
        if not ok:
            safe_print(f"POST {endpoint} failed: {r.status} {txt[:400]}")
        return ok


async def main() -> None:
    ap = argparse.ArgumentParser()
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

        staples = pantry_staples(await get_result(session, lim, headers, "/v2/sync/pantry/"))
        lists = [x for x in await get_result(session, lim, headers, "/v2/sync/grocerylists/")
                 if not x.get("deleted")]
        main_list = next((x for x in lists if x.get("is_default")), lists[0] if lists else None)
        if not main_list:
            raise SystemExit("no grocery list found")
        list_uid = main_list["uid"]
        aisles = {
            a.get("name"): a.get("uid")
            for a in await get_result(session, lim, headers, "/v2/sync/groceryaisles/")
            if not a.get("deleted") and a.get("name")
        }
        groceries = await get_result(session, lim, headers, "/v2/sync/groceries/")
        on_list = {
            name_key(g.get(field))
            for g in groceries
            if not g.get("deleted") and not g.get("purchased") and g.get("list_uid") == list_uid
            for field in ("name", "ingredient")
        }

        safe_print(f"Grocery list: {main_list.get('name')} ({len(staples)} staples in pantry)")
        adds = []
        for x in sorted(staples, key=lambda r: r.get("ingredient") or ""):
            name = x["ingredient"][len(STAPLE_PREFIX):].strip()
            qty = str(x.get("quantity") or "").strip()
            aisle = x.get("aisle") or "Miscellaneous"
            if name_key(name) in on_list:
                safe_print(f"  SKIP    {name} (already on the list)")
                continue
            safe_print(f"  ADD     {name} | {qty or '-'} | {aisle}")
            adds.append({
                "uid": str(uuid.uuid4()).upper(),
                "name": name,
                "ingredient": name,
                "quantity": qty,
                "aisle": aisle,
                "aisle_uid": x.get("aisle_uid") or aisles.get(aisle) or aisles.get("Miscellaneous"),
                "purchased": False,
                "recipe": "",
                "recipe_uid": None,
                "instruction": "",
                "separate": False,
                "list_uid": list_uid,
                "order_flag": 0,
            })

        pending = local_pending_groceries()
        if pending:
            safe_print(
                f"Local SQLite has {pending} pending grocery row(s); the desktop app "
                "would overwrite this. See docs/desktop-sync-troubleshooting.md."
            )
            if args.apply:
                raise SystemExit("not applied")

        if not adds:
            safe_print("Nothing to add.")
            return
        if not args.apply:
            safe_print(f"Dry-run only: would add {len(adds)}. Pass --apply to write.")
            return

        for i in range(0, len(adds), 20):
            if not await post_entities(session, lim, headers, "/v2/sync/groceries/", adds[i:i + 20]):
                raise SystemExit(f"grocery add failed at {i}")
        safe_print(f"Added {len(adds)} staple(s)")

        await lim.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            safe_print("notify", r.status)


if __name__ == "__main__":
    asyncio.run(main())
