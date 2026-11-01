"""Paprika pantry update through the sync API.

Two modes:
- Targeted: change quantities by uid (UID=QTY) and add items (--add). Rows not
  named are never posted.
- Full stock (--stock / --stock-file): the list is the whole pantry. Listed
  names (exact match) get their quantity updated or are added; any current row
  not listed has been used up and is soft-deleted (deleted, not in stock).

Changed rows keep every field but quantity (and deleted/in_stock for deletes).
Dry-run unless --apply. Refuses to apply while the local SQLite has pending
pantry rows, since the desktop app would overwrite the change; it never writes
the local SQLite (see docs/desktop-sync-troubleshooting.md).

  python scripts/tools/update_pantry_quantities.py UID=QTY [UID=QTY ...]
  python scripts/tools/update_pantry_quantities.py --add "Name=4" --apply
  python scripts/tools/update_pantry_quantities.py --add "Staple - Eggs=12|Dairy" --notes staple
  python scripts/tools/update_pantry_quantities.py --stock "Name=4" --stock "Other=2"
  python scripts/tools/update_pantry_quantities.py --stock-file stock.txt --apply
  python scripts/tools/update_pantry_quantities.py --list
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

# Staples are always present; a full-stock list never deletes them.
STAPLE_PREFIX = "Staple - "

FROZEN_AISLE = "Frozen Foods"
FROZEN_AISLE_UID = (
    "37CEE1DBD7FFC9FFE189BB1390B090778D58B4E8BA108C179A054664A98D2BDA"
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def parse_pair(s: str) -> tuple[str, str]:
    key, sep, qty = s.rpartition("=")
    if not sep or not key.strip() or not qty.strip():
        raise SystemExit(f"expected KEY=QTY, got {s!r}")
    return key.strip(), qty.strip()


def print_rows(rows: list) -> None:
    for x in rows:
        safe_print(
            f"  qty={x.get('quantity')!r}  {x.get('ingredient')}  "
            f"[{x.get('aisle')}]  in_stock={x.get('in_stock')}  "
            f"deleted={x.get('deleted')}  uid={x.get('uid')}"
        )


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


async def get_pantry(session, lim, headers) -> list:
    st, body = await api_json(
        session, lim, "GET", f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers
    )
    if st != 200:
        raise SystemExit(f"pantry GET failed {st}")
    return (body or {}).get("result") or []


def local_pending_pantry() -> int:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            """
            select count(*) from pantry_items
            where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0
            """
        ).fetchone()[0]
    finally:
        con.close()


def read_stock_file(path: str) -> list[tuple[str, str]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [parse_pair(s) for s in lines if s.strip() and not s.lstrip().startswith("#")]


def local_aisles() -> dict[str, str]:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        return {name: uid for uid, name in con.execute("select uid, name from grocery_aisles")}
    finally:
        con.close()


def print_add(row: dict) -> None:
    safe_print(f"  ADD     {row['quantity']}x {row['ingredient']} [{row['aisle']}]")


def new_item(name: str, qty: str, args) -> dict:
    # QTY may carry its own aisle: "Name=1|Dairy"
    aisle, aisle_uid = args.aisle, args.aisle_uid
    if "|" in qty:
        qty, aisle = (s.strip() for s in qty.split("|", 1))
        aisles = local_aisles()
        if aisle not in aisles:
            raise SystemExit(f"unknown aisle {aisle!r}; known: {sorted(aisles)}")
        aisle_uid = aisles[aisle]
    return {
        "uid": str(uuid.uuid4()),
        "ingredient": name,
        "aisle": aisle,
        "aisle_uid": aisle_uid,
        "expiration_date": None,
        "has_expiration": False,
        "in_stock": True,
        "purchase_date": None,
        "quantity": qty,
        "location_uid": None,
        "notes": args.notes,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("changes", nargs="*", help="UID=QTY pairs")
    ap.add_argument("--add", action="append", default=[], metavar="NAME=QTY",
                    help="new item (Frozen Foods aisle unless --aisle)")
    ap.add_argument("--stock", action="append", default=[], metavar="NAME=QTY",
                    help="full stock list: rows not listed are deleted as used up")
    ap.add_argument("--stock-file", help="full stock list, one NAME=QTY per line")
    ap.add_argument("--aisle", default=FROZEN_AISLE)
    ap.add_argument("--aisle-uid", default=FROZEN_AISLE_UID)
    ap.add_argument("--notes", default="freezer portion(s)")
    ap.add_argument("--list", action="store_true", help="only print the pantry")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    changes = dict(parse_pair(c) for c in args.changes)
    adds = [parse_pair(a) for a in args.add]
    stock = [parse_pair(s) for s in args.stock]
    if args.stock_file:
        stock += read_stock_file(args.stock_file)
    if stock and (changes or adds):
        raise SystemExit("--stock replaces UID=QTY and --add; use one or the other")

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

        live = await get_pantry(session, lim, headers)
        if args.list or (not changes and not adds and not stock):
            safe_print(f"Pantry rows: {len(live)}")
            print_rows(live)
            return

        active = [x for x in live if not x.get("deleted")]
        updates, new_rows, deletes = [], [], []

        if stock:
            # Exhaustive list: match by exact name; anything active and not
            # listed has been used up and is soft-deleted.
            by_name = {(x.get("ingredient") or "").strip(): x for x in active}
            listed = set()
            for name, qty in stock:
                row = by_name.get(name)
                if row is None:
                    new_rows.append(new_item(name, qty, args))
                    print_add(new_rows[-1])
                    continue
                listed.add(name)
                qty = qty.split("|", 1)[0].strip()
                if str(row.get("quantity")) == qty:
                    safe_print(f"  SAME    {qty}x {name}")
                    continue
                safe_print(f"  UPDATE  {name}: {row.get('quantity')!r} -> {qty!r}")
                row = dict(row)
                row["quantity"] = qty
                updates.append(row)
            for name, row in by_name.items():
                if name not in listed and name.startswith(STAPLE_PREFIX):
                    safe_print(f"  KEEP    {name} (staple, always present)")
                elif name not in listed:
                    safe_print(f"  DELETE  {name} (was {row.get('quantity')!r}; not in list, used up)")
                    row = dict(row)
                    row["deleted"] = True
                    row["in_stock"] = False
                    deletes.append(row)
        else:
            by_uid = {x.get("uid"): x for x in live}
            missing = [u for u in changes if u not in by_uid]
            if missing:
                raise SystemExit(f"uid(s) not in pantry: {missing}")
            for u, qty in changes.items():
                row = dict(by_uid[u])
                safe_print(f"  UPDATE  {row.get('ingredient')}: {row.get('quantity')!r} -> {qty!r}")
                row["quantity"] = qty
                updates.append(row)
            names = {(x.get("ingredient") or "").strip() for x in active}
            for name, qty in adds:
                if name in names:
                    raise SystemExit(f"already in pantry, update by uid instead: {name}")
                new_rows.append(new_item(name, qty, args))
                print_add(new_rows[-1])

        pending = local_pending_pantry()
        if pending:
            safe_print(
                f"Local SQLite has {pending} pending pantry row(s); the desktop app "
                "would overwrite this. See docs/desktop-sync-troubleshooting.md."
            )
            if args.apply:
                raise SystemExit("not applied")

        payload = updates + new_rows + deletes
        if not payload:
            safe_print("Nothing to change.")
            return
        if not args.apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        if not await post_entities(session, lim, headers, "/v2/sync/pantry/", payload):
            raise SystemExit("pantry update failed")
        safe_print(
            f"Posted {len(updates)} update(s), {len(new_rows)} new, {len(deletes)} deleted"
        )

        await lim.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            safe_print("notify", r.status)

        after = await get_pantry(session, lim, headers)
        safe_print(f"Pantry after update: {len(after)} rows")
        print_rows(after)


if __name__ == "__main__":
    asyncio.run(main())
