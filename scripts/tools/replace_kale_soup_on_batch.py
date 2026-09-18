"""Replace Kale soup on the single Batch menu with another eligible soup."""
from __future__ import annotations

import asyncio
import gzip
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
from rebuild_batch_menus_mix import BATCH_SCALE, is_our_menu, parse_servings  # noqa: E402
from rebuild_single_batch_menu import MENU_NAME  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def pick_soup(exclude_uids: set[str], exclude_bits: set[str]) -> dict:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT DISTINCT r.uid, r.name, r.servings
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE coalesce(r.in_trash, 0) = 0
          AND c.name = 'Soups'
          AND (r.name LIKE '✅%' OR r.name LIKE '⚠️%')
          AND lower(r.name) GLOB '*soup*'
          AND lower(r.name) NOT GLOB '*broth*'
          AND lower(r.name) NOT GLOB '*kale*'
        ORDER BY
          CASE WHEN r.name LIKE '✅%' THEN 0 ELSE 1 END,
          r.name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    preferred = [
        "tomato basil",
        "corn chowder",
        "chicken noodle",
        "broccoli cheddar",
        "potato soup",
        "greek lemon",
        "pumpkin tomato",
        "egg drop",
        "tomato soup with meatballs",
        "clam chowder",
        "chicken stew",
    ]
    ranked = []
    for r in rows:
        uid = (r["uid"] or "").upper()
        name = r["name"] or ""
        if uid in exclude_uids:
            continue
        low = name.lower()
        if any(b in low for b in exclude_bits if b):
            continue
        pref = next((i for i, p in enumerate(preferred) if p in low), 99)
        ranked.append(
            {
                "uid": r["uid"],
                "name": name,
                "servings": parse_servings(r["servings"]),
                "pref": pref,
            }
        )
    ranked.sort(key=lambda x: (x["pref"], 0 if x["name"].startswith("✅") else 1, x["name"]))
    if not ranked:
        raise SystemExit("No replacement soup found")
    return ranked[0]


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
            if not m.get("deleted")
            and (
                (m.get("name") or "").strip() == MENU_NAME
                or is_our_menu(m.get("name") or "")
            )
        ]
        menu_uids = {(m.get("uid") or "").upper() for m in menus}
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        items = [
            it
            for it in (body["result"] or [])
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper() in menu_uids
        ]

        kale_items = [
            it
            for it in items
            if "kale" in (it.get("name") or "").lower()
        ]
        if not kale_items:
            raise SystemExit("No kale soup item found on Batch menu")

        used = {(it.get("recipe_uid") or "").upper() for it in items}
        bits = set()
        for it in items:
            n = (it.get("name") or "").lower()
            for b in ("wild rice", "carrot ginger", "tomato basil", "kale"):
                if b in n:
                    bits.add(b)

        replacement = pick_soup(used, bits)
        safe_print(f"Replace: {kale_items[0].get('name')}")
        safe_print(f"With:    {replacement['name']}")

        if not apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        updated = []
        for it in kale_items:
            updated.append(
                {
                    **it,
                    "recipe_uid": replacement["uid"],
                    "name": replacement["name"],
                    "scale": f"{BATCH_SCALE}/{replacement['servings']}",
                }
            )
        if not await post_entities(s, limiter, H, "/v2/sync/menuitems/", updated):
            raise SystemExit("menuitem update failed")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
        safe_print("Done")


if __name__ == "__main__":
    asyncio.run(main())
