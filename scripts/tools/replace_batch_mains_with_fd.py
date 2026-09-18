"""Replace all Batch menu main (Dinner) slots with Field Doctor meals.

Keeps soups and snacks. Soft-requires ✅/⚠️ Field Doctor recipes.

  python scripts/tools/replace_batch_mains_with_fd.py
  python scripts/tools/replace_batch_mains_with_fd.py --apply
"""
from __future__ import annotations

import asyncio
import gzip
import json
import random
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
from rebuild_batch_menus_mix import (  # noqa: E402
    BATCH_SCALE,
    MENU_NOTES,
    is_our_menu,
    parse_servings,
)

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def list_fd_mains(n: int, exclude: set[str]) -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT DISTINCT r.uid, r.name, r.servings
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE coalesce(r.in_trash, 0) = 0
          AND c.name = 'Field Doctor'
          AND (r.name LIKE '✅%' OR r.name LIKE '⚠️%')
          AND lower(r.name) LIKE '%field doctor%'
        ORDER BY r.name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    pool = [
        {
            "uid": r["uid"],
            "name": r["name"],
            "servings": parse_servings(r["servings"]),
        }
        for r in rows
        if (r["uid"] or "").upper() not in exclude
    ]
    # Prefer variety: shuffle with fixed seed then take n
    random.seed(42)
    random.shuffle(pool)
    # Light preference: mix protein styles by sorting key groups after shuffle
    # Already shuffled — take first n unique
    if len(pool) < n:
        raise SystemExit(f"Need {n} Field Doctor meals, found {len(pool)}")
    return pool[:n]


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
            safe_print(f"POST {endpoint} failed: {r.status} {txt[:300]}")
        return ok


async def main() -> None:
    apply = "--apply" in sys.argv
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
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/mealtypes/", headers=H
        )
        types = {t["name"]: t["uid"] for t in body["result"] if not t.get("deleted")}
        dinner_uid = (types.get("Dinner") or "").upper()
        if not dinner_uid:
            raise SystemExit("Dinner meal type missing")

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and is_our_menu(m.get("name") or "")
        ]
        menu_by_uid = {(m.get("uid") or "").upper(): m for m in menus}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        items = [
            it
            for it in (body["result"] or [])
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper() in menu_by_uid
        ]

        def batch_num(menu_name: str) -> int:
            m = re.search(r"Batch\s+(\d+)", menu_name or "")
            return int(m.group(1)) if m else 999

        mains = [
            it
            for it in items
            if (it.get("type_uid") or "").upper() == dinner_uid
        ]
        mains.sort(
            key=lambda it: (
                batch_num(menu_by_uid[(it.get("menu_uid") or "").upper()].get("name")),
                it.get("order_flag") or 0,
            )
        )
        safe_print(f"Main (Dinner) slots to replace: {len(mains)}")
        for it in mains:
            m = menu_by_uid[(it.get("menu_uid") or "").upper()]
            safe_print(f"  {m.get('name')}: {it.get('name')}")

        keep_uids = {
            (it.get("recipe_uid") or "").upper()
            for it in items
            if (it.get("type_uid") or "").upper() != dinner_uid
        }
        fd = list_fd_mains(len(mains), keep_uids)
        safe_print("\nNew Field Doctor mains:")
        for i, rec in enumerate(fd):
            safe_print(f"  {i+1}. {rec['name']}")

        if not apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        updated_items = []
        for it, rec in zip(mains, fd):
            new_it = {
                **it,
                "recipe_uid": rec["uid"],
                "name": rec["name"],
                "scale": f"{BATCH_SCALE}/{rec['servings']}",
            }
            updated_items.append(new_it)
            m = menu_by_uid[(it.get("menu_uid") or "").upper()]
            safe_print(f"  {m.get('name')}: {it.get('name')[:40]} → {rec['name'][:40]}")

        chunk = 6
        for i in range(0, len(updated_items), chunk):
            part = updated_items[i : i + chunk]
            if not await post_entities(s, limiter, H, "/v2/sync/menuitems/", part):
                raise SystemExit(f"menuitems chunk {i} failed")
            safe_print(f"  posted items {i+1}-{i+len(part)}")

        # Refresh all menu notes from current item set
        by_menu: dict[str, list] = {}
        name_map = {(u.get("uid") or "").upper(): u["name"] for u in updated_items}
        for it in items:
            mid = (it.get("menu_uid") or "").upper()
            iuid = (it.get("uid") or "").upper()
            by_menu.setdefault(mid, []).append(
                (it.get("order_flag") or 0, name_map.get(iuid, it.get("name") or ""))
            )

        updated_menus = []
        for m in menus:
            mid = (m.get("uid") or "").upper()
            kids = sorted(by_menu.get(mid, []), key=lambda x: x[0])
            names = [n for _, n in kids]
            notes = MENU_NOTES + (f"\n\nRecipes: {' + '.join(names)}" if names else "")
            if (m.get("notes") or "") != notes:
                updated_menus.append({**m, "notes": notes})

        if updated_menus:
            if not await post_entities(s, limiter, H, "/v2/sync/menus/", updated_menus):
                raise SystemExit("menus update failed")
            safe_print(f"Updated {len(updated_menus)} menu notes")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
        safe_print("Done")


if __name__ == "__main__":
    asyncio.run(main())
