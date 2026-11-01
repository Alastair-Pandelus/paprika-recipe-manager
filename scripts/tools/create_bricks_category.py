"""Create the Bricks folder tree (Lego-style component recipes) under Low Fodmap in Paprika.

Adds, if missing:
  Low Fodmap > Bricks            (ordered after the existing Low Fodmap folders)
  Low Fodmap > Bricks > Proteins, Carbs, Vegetables, Sauces, Meals

Creates categories only; no recipes are touched. Existing folders are left alone, so it is
safe to re-run (for example after adding a name to CHILDREN). Dry run unless --apply.

  python scripts/tools/create_bricks_category.py [--apply]
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

PARENT_NAME = "Low Fodmap"
FOLDER_NAME = "Bricks"
CHILDREN = ["Proteins", "Carbs", "Vegetables", "Sauces", "Meals"]


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def plan(cats: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (new category rows, report lines) for the given live categories."""
    low = next(c for c in cats if c["name"] == PARENT_NAME and not c.get("parent_uid"))
    siblings = [c for c in cats if c.get("parent_uid") == low["uid"]]
    new: list[dict] = []
    lines: list[str] = []

    bricks = next((c for c in siblings if c["name"] == FOLDER_NAME), None)
    if bricks:
        lines.append(f"EXISTS {PARENT_NAME} > {FOLDER_NAME}: {bricks['uid']}")
    else:
        bricks = {
            "uid": str(uuid.uuid4()).upper(),
            "name": FOLDER_NAME,
            "parent_uid": low["uid"],
            "order_flag": max([c.get("order_flag") or 0 for c in siblings] + [0]) + 1,
            "deleted": False,
        }
        new.append(bricks)
        lines.append(f"NEW {PARENT_NAME} > {FOLDER_NAME} (order {bricks['order_flag']})")

    kids = [c for c in cats if c.get("parent_uid") == bricks["uid"]]
    for i, name in enumerate(CHILDREN):
        if any(c["name"] == name for c in kids):
            lines.append(f"EXISTS {FOLDER_NAME} > {name}")
            continue
        new.append({"uid": str(uuid.uuid4()).upper(), "name": name, "parent_uid": bricks["uid"],
                    "order_flag": i, "deleted": False})
        lines.append(f"NEW {PARENT_NAME} > {FOLDER_NAME} > {name} (order {i})")
    return new, lines


async def get_categories(session, headers) -> list[dict]:
    async with session.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
        return [c for c in (await r.json())["result"] if not c.get("deleted")]


async def main() -> None:
    apply = "--apply" in sys.argv
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": password}) as r:
            body = await r.json()
        if "result" not in (body or {}):
            raise SystemExit(f"login refused: {body}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        new, lines = plan(await get_categories(session, headers))
        print("\n".join(lines))
        if not new:
            print("nothing to create")
            return
        if not apply:
            print("dry run: re-run with --apply to create it")
            return

        form = aiohttp.FormData()
        form.add_field("data", gzip_json(new), content_type="application/octet-stream", filename="data")
        async with session.post(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form) as r:
            text = await r.text()
        if '"result":true' not in text.replace(" ", ""):
            raise SystemExit(f"category create failed: {text[:300]}")
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        after = {c["uid"]: c for c in await get_categories(session, headers)}
        for row in new:
            got = after.get(row["uid"])
            assert got and got["name"] == row["name"] and got["parent_uid"] == row["parent_uid"], f"read-back failed: {row['name']}"
        print(f"CREATED {len(new)} categories")


if __name__ == "__main__":
    asyncio.run(main())
