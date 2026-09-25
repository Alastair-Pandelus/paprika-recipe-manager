"""Refresh Batch menu item names + notes to current recipe title markers (✅ℹ️❌).

Keeps existing recipe picks; only rewrites displayed names and menu notes.

  python scripts/tools/refresh_batch_menu_title_emojis.py
  python scripts/tools/refresh_batch_menu_title_emojis.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from rebuild_batch_menus_mix import is_our_menu  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

MENU_NOTES = (
    "Low FODMAP batch cook (✅ℹ️ only — no ❌). "
    "Mix ~60% main meals / 20% soups / 20% snacks. "
    "Scale each recipe to 8 portions (recipes are 1-serve). "
    "Both dishes under Day 1. Lunch + dinner from freezer; breakfast separate."
)

_TITLE_EMOJI_RE = re.compile(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*")


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def recipe_names_by_uid() -> dict[str, str]:
    con = sqlite3.connect(LOCAL_DB)
    rows = con.execute(
        "SELECT uid, name FROM recipes WHERE coalesce(in_trash,0)=0"
    ).fetchall()
    con.close()
    return {(u or "").upper(): n or "" for u, n in rows}


async def post_entities(session, limiter, headers, endpoint: str, items: list[dict]) -> bool:
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
            safe_print(f"POST {endpoint} failed: {r.status} {txt[:300]}")
        return ok


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)

    names = recipe_names_by_uid()
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)

    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and is_our_menu(m.get("name") or "")
        ]
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        items_all = body["result"] or []
        menu_uids = {(m.get("uid") or "").upper() for m in menus}
        items = [
            it
            for it in items_all
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper() in menu_uids
        ]

        safe_print(f"Menus={len(menus)} items={len(items)} | mode={'APPLY' if apply else 'DRY-RUN'}")

        updated_items: list[dict] = []
        for it in items:
            uid = (it.get("recipe_uid") or "").upper()
            new_name = names.get(uid)
            if not new_name:
                safe_print(f"  skip item missing recipe {it.get('name')}")
                continue
            old = it.get("name") or ""
            if old != new_name:
                safe_print(f"  item: {old[:50]} → {new_name[:50]}")
                updated_items.append({**it, "name": new_name})

        # Refresh menu notes from current item names (preserve order_flag)
        by_menu: dict[str, list[dict]] = {}
        for it in items:
            by_menu.setdefault((it.get("menu_uid") or "").upper(), []).append(it)

        updated_menus: list[dict] = []
        for m in menus:
            mid = (m.get("uid") or "").upper()
            kids = sorted(
                by_menu.get(mid, []),
                key=lambda x: (x.get("order_flag") or 0, x.get("name") or ""),
            )
            # Prefer updated names when we have them
            name_map = {
                (u.get("uid") or "").upper(): u["name"] for u in updated_items
            }
            display = []
            for it in kids:
                iuid = (it.get("uid") or "").upper()
                ruid = (it.get("recipe_uid") or "").upper()
                display.append(
                    name_map.get(iuid) or names.get(ruid) or (it.get("name") or "")
                )
            notes = MENU_NOTES
            if display:
                notes = MENU_NOTES + f"\n\nRecipes: {' + '.join(display)}"
            old_notes = m.get("notes") or ""
            if old_notes != notes:
                safe_print(f"  menu {m.get('name')}: notes refresh")
                updated_menus.append({**m, "notes": notes})

        safe_print(
            f"Would update items={len(updated_items)} menus={len(updated_menus)}"
        )
        if not apply:
            return

        if updated_items:
            chunk = 8
            for i in range(0, len(updated_items), chunk):
                part = updated_items[i : i + chunk]
                if not await post_entities(
                    s, limiter, H, "/v2/sync/menuitems/", part
                ):
                    raise SystemExit(f"menuitems chunk {i} failed")
                safe_print(f"  posted items {i + 1}-{i + len(part)}")

        if updated_menus:
            if not await post_entities(s, limiter, H, "/v2/sync/menus/", updated_menus):
                raise SystemExit("menus update failed")
            safe_print(f"  posted {len(updated_menus)} menus")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
        safe_print("Done")


if __name__ == "__main__":
    asyncio.run(main())
