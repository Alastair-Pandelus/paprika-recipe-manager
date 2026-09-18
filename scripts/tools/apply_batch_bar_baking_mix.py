"""Apply 60/20/10/10 mix on live Batch: keep days 5–6 bars; set days 7–8 to baking."""
from __future__ import annotations

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
from rebuild_batch_menus_mix import BATCH_SCALE, MENU_NOTES as MIX_NOTES, parse_servings, pool_for  # noqa: E402
from rebuild_single_batch_menu import MENU_NAME, MENU_NOTES  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post(session, headers, limiter, endpoint, items) -> bool:
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
        return '"result":true' in txt.replace(" ", "")


async def main() -> None:
    apply = "--apply" in sys.argv
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.3)

    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and (m.get("name") or "").strip() == MENU_NAME
        ]
        if not menus:
            raise SystemExit("Batch menu missing")
        menu = menus[0]
        mid = (menu.get("uid") or "").upper()

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/mealtypes/", headers=H
        )
        types = {t["name"]: t["uid"] for t in body["result"] if not t.get("deleted")}
        snacks_uid = (types.get("Snacks") or "").upper()

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        items = [
            it
            for it in (body["result"] or [])
            if not it.get("deleted") and (it.get("menu_uid") or "").upper() == mid
        ]

        # Days 7–8 snack slots → baking
        targets = [
            it
            for it in items
            if (it.get("type_uid") or "").upper() == snacks_uid
            and int(it.get("day") or 0) in (7, 8)
        ]
        targets.sort(key=lambda it: int(it.get("day") or 0))
        used = {(it.get("recipe_uid") or "").upper() for it in items}
        baking = [
            r
            for r in pool_for("baking")
            if r["uid"].upper() not in used
        ][: len(targets)]
        if len(baking) < len(targets):
            raise SystemExit("Not enough baking recipes")

        safe_print("Notes → 60/20/10/10")
        for it, rec in zip(targets, baking):
            safe_print(f"  Day {it.get('day')}: {it.get('name')} → {rec['name']}")

        if not apply:
            safe_print("Dry-run only. Pass --apply")
            return

        updated_items = []
        for it, rec in zip(targets, baking):
            updated_items.append(
                {
                    **it,
                    "recipe_uid": rec["uid"],
                    "name": rec["name"],
                    "scale": f"{BATCH_SCALE}/{rec['servings']}",
                }
            )
        if not await post(s, H, limiter, "/v2/sync/menuitems/", updated_items):
            raise SystemExit("items failed")

        # Prefer single-batch notes wording
        if not await post(
            s, H, limiter, "/v2/sync/menus/", [{**menu, "notes": MENU_NOTES}]
        ):
            raise SystemExit("notes failed")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
        safe_print("Done")


if __name__ == "__main__":
    asyncio.run(main())
