"""Delete Soup - Vegetable Broth and replace it in Batch menus with another soup."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
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
from rebuild_batch_menus_mix import (  # noqa: E402
    BATCH_SCALE,
    MENU_NOTES,
    is_our_menu,
    parse_servings,
)

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
BROTH_UID = "CC3AAC0C-134B-4AEF-838E-B72ED1ED1D20"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def pick_replacement(exclude_uids: set[str], exclude_name_bits: set[str]) -> dict:
    """Prefer ✅ soups (not broths) not already on a batch menu."""
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
          AND r.name LIKE '✅%'
          AND r.uid != ?
          AND lower(r.name) GLOB '*soup*'
          AND lower(r.name) NOT GLOB '*broth*'
        ORDER BY r.name COLLATE NOCASE
        """,
        (BROTH_UID,),
    ).fetchall()
    con.close()

    # Prefer distinctive batch-friendly soups first
    preferred = [
        "tomato basil",
        "corn chowder",
        "chicken noodle",
        "broccoli cheddar",
        "potato soup",
        "greek lemon",
        "pumpkin tomato",
        "egg drop",
    ]

    def ok(name: str, uid: str) -> bool:
        if uid.upper() in exclude_uids:
            return False
        low = name.lower()
        for bit in exclude_name_bits:
            if bit and bit in low:
                return False
        return True

    ranked: list[dict] = []
    for r in rows:
        uid = r["uid"] or ""
        name = r["name"] or ""
        if not ok(name, uid):
            continue
        low = name.lower()
        pref = next((i for i, p in enumerate(preferred) if p in low), 99)
        ranked.append(
            {
                "uid": uid,
                "name": name,
                "servings": parse_servings(r["servings"]),
                "pref": pref,
            }
        )
    ranked.sort(key=lambda x: (x["pref"], x["name"]))
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

        used = {(it.get("recipe_uid") or "").upper() for it in items}
        used_bits: set[str] = set()
        for it in items:
            n = (it.get("name") or "").lower()
            for bit in (
                "wild rice",
                "kale, potato",
                "carrot ginger",
                "roasted carrot-ginger",
                "vegetable broth",
            ):
                if bit in n:
                    used_bits.add(bit)
            if "carrot" in n and "ginger" in n:
                used_bits.add("carrot ginger")

        broth_items = [
            it
            for it in items
            if (it.get("recipe_uid") or "").upper() == BROTH_UID.upper()
            or "Vegetable Broth" in (it.get("name") or "")
        ]
        safe_print(f"Menus={len(menus)} items={len(items)} broth_slots={len(broth_items)}")
        for it in broth_items:
            mname = next(
                (
                    m["name"]
                    for m in menus
                    if (m.get("uid") or "").upper()
                    == (it.get("menu_uid") or "").upper()
                ),
                "?",
            )
            safe_print(f"  {mname}: {it.get('name')}")

        replacement = pick_replacement(used, used_bits)
        safe_print(f"Replacement: {replacement['name']}")

        if not apply:
            safe_print("Dry-run only. Pass --apply to delete + swap.")
            return

        # 1) Update menu items to replacement
        updated_items = []
        for it in broth_items:
            new_it = {
                **it,
                "recipe_uid": replacement["uid"],
                "name": replacement["name"],
            }
            if replacement["servings"] != BATCH_SCALE:
                new_it["scale"] = f"{BATCH_SCALE}/{replacement['servings']}"
            elif "scale" in new_it:
                # keep or clear — set explicit 8/1
                new_it["scale"] = f"{BATCH_SCALE}/{replacement['servings']}"
            updated_items.append(new_it)

        if updated_items:
            if not await post_entities(
                s, limiter, H, "/v2/sync/menuitems/", updated_items
            ):
                raise SystemExit("menuitem update failed")
            safe_print(f"Updated {len(updated_items)} menu item(s)")

        # 2) Refresh notes on affected menus
        by_menu: dict[str, list] = {}
        for it in items:
            mid = (it.get("menu_uid") or "").upper()
            # use updated name if swapped
            name = it.get("name")
            for u in updated_items:
                if (u.get("uid") or "").upper() == (it.get("uid") or "").upper():
                    name = u["name"]
                    break
            by_menu.setdefault(mid, []).append((it.get("order_flag") or 0, name))

        updated_menus = []
        affected = {(it.get("menu_uid") or "").upper() for it in broth_items}
        for m in menus:
            mid = (m.get("uid") or "").upper()
            if mid not in affected:
                continue
            kids = sorted(by_menu.get(mid, []), key=lambda x: x[0])
            names = [n for _, n in kids]
            notes = MENU_NOTES + (f"\n\nRecipes: {' + '.join(names)}" if names else "")
            updated_menus.append({**m, "notes": notes})
        if updated_menus:
            if not await post_entities(s, limiter, H, "/v2/sync/menus/", updated_menus):
                raise SystemExit("menu notes update failed")
            safe_print(f"Updated {len(updated_menus)} menu note(s)")

        # 3) Soft-delete the recipe (cloud + local trash)
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{BROTH_UID}/", headers=H
        )
        rec = (body or {}).get("result") or {}
        if rec.get("uid"):
            rec["deleted"] = True
            rec["in_trash"] = True
            rec["hash"] = calc_hash(rec)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{BROTH_UID}/", headers=H, data=form
            ) as r:
                txt = await r.text()
                ok = '"result":true' in txt.replace(" ", "")
            if not ok:
                raise SystemExit(f"recipe delete failed: {txt[:300]}")
            safe_print("Cloud recipe marked deleted/in_trash")
        else:
            safe_print("Cloud recipe already missing")

        con = sqlite3.connect(LOCAL_DB)
        con.execute(
            "UPDATE recipes SET in_trash=1, status=? WHERE uid=?",
            ("modified", BROTH_UID),
        )
        con.commit()
        con.close()
        safe_print("Local recipe in_trash=1")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
        safe_print(f"Done. Batch now uses: {replacement['name']}")


if __name__ == "__main__":
    asyncio.run(main())
