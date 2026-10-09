"""Replace Paprika pantry with freezer meal stock (portions).

Clears existing pantry items, then adds freezer Favourites with quantities.

  python scripts/tools/set_freezer_pantry.py
  python scripts/tools/set_freezer_pantry.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
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

# Freezer stock: (ingredient display name matching Favourites, quantity portions)
FREEZER_STOCK = [
    ("✅ ♡ Field Doctor Potato Topped Chicken Pie (Low FODMAP)", "1"),
    ("ℹ️ ♡ Creamy Carrot Hummus (My Go-To Low FODMAP Snack!)", "9"),
    ("✅ ♡ Field Doctor Beef Bolognese + Penne (Low FODMAP)", "7"),
]

FROZEN_AISLE = "Frozen Foods"
FROZEN_AISLE_UID = (
    "37CEE1DBD7FFC9FFE189BB1390B090778D58B4E8BA108C179A054664A98D2BDA"
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_entities(session, limiter, headers, endpoint: str, items: list) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(items),
        content_type="application/octet-stream",
        filename="data",
    )
    await limiter.wait_turn()
    async with session.post(
        f"{PAPRIKA_API}{endpoint}", headers=headers, data=form
    ) as r:
        txt = await r.text()
        ok = '"result":true' in txt.replace(" ", "")
        if not ok:
            safe_print(f"POST {endpoint} failed: {r.status} {txt[:400]}")
        return ok


def new_item(ingredient: str, quantity: str) -> dict:
    return {
        "uid": str(uuid.uuid4()),
        "ingredient": ingredient,
        "aisle": FROZEN_AISLE,
        "aisle_uid": FROZEN_AISLE_UID,
        "expiration_date": None,
        "has_expiration": False,
        "in_stock": True,
        "purchase_date": None,
        "quantity": quantity,
        "location_uid": None,
        "notes": "freezer portion(s)",
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    safe_print("Will clear pantry and set freezer stock:")
    for name, qty in FREEZER_STOCK:
        safe_print(f"  {qty}× {name}")

    if not args.apply:
        safe_print("Dry-run only. Pass --apply to write.")
        return

    user, pw = paprika_credentials()
    lim = RateLimiter(0.28)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session,
            lim,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            session, lim, "GET", f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers
        )
        existing = (body or {}).get("result") or []
        safe_print(f"Existing pantry items: {len(existing)}")

        # Soft-delete existing (same pattern as groceries/meals)
        if existing:
            deletes = []
            for it in existing:
                row = dict(it)
                row["deleted"] = True
                row["in_stock"] = False
                deletes.append(row)
            for i in range(0, len(deletes), 20):
                chunk = deletes[i : i + 20]
                if not await post_entities(
                    session, lim, headers, "/v2/sync/pantry/", chunk
                ):
                    raise SystemExit("pantry delete failed")
            safe_print(f"Deleted {len(deletes)}")

        adds = [new_item(name, qty) for name, qty in FREEZER_STOCK]
        if not await post_entities(session, lim, headers, "/v2/sync/pantry/", adds):
            raise SystemExit("pantry add failed")
        safe_print(f"Added {len(adds)}")

        await lim.wait_turn()
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
        ) as r:
            safe_print("notify", r.status)

        st, body = await api_json(
            session, lim, "GET", f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers
        )
        live = (body or {}).get("result") or []
        # Keep only in-stock / non-deleted if API returns all
        active = [
            x
            for x in live
            if x.get("in_stock") and not x.get("deleted")
        ]
        if not active and live:
            # API may omit deleted; show all returned
            active = live
        safe_print(f"Live pantry after sync: {len(active)}")
        for x in active:
            safe_print(
                f"  qty={x.get('quantity')!r}  {x.get('ingredient')}  "
                f"[{x.get('aisle')}]"
            )

        # Mirror into local SQLite so reads match until desktop sync
        con = sqlite3.connect(LOCAL_DB)
        con.execute("DELETE FROM pantry_items")
        for x in adds:
            con.execute(
                """
                INSERT INTO pantry_items (
                    uid, status, quantity, ingredient, aisle_name, aisle_uid,
                    purchase_date, expiration_date, has_expiration, in_stock,
                    is_synced, sync_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    x["uid"],
                    "modified",
                    x["quantity"],
                    x["ingredient"],
                    x["aisle"],
                    x["aisle_uid"],
                    None,
                    None,
                    0,
                    1,
                    0,
                    None,
                ),
            )
        con.commit()
        n = con.execute("SELECT count(*) FROM pantry_items").fetchone()[0]
        con.close()
        safe_print(f"Local pantry_items rows: {n}")


if __name__ == "__main__":
    asyncio.run(main())
