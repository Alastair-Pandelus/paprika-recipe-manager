"""Rebuild Batch 1–10 menus using only non-🔴 FODMAP-scored recipes."""
from __future__ import annotations

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

# kind, uid — names loaded live; exclude 🔴 meal scores only
PLAN_UIDS: list[tuple[str, str]] = [
    # Batch 1
    ("main", "6B4663D8-8170-447D-9B4E-6B7035FF09AE"),  # FD Chicken Korma
    ("soup", "56C43E8E-9443-4E49-81D4-DB2267415E31"),  # roasted pumpkin soup
    # Batch 2
    ("main", "B7A0C22C-577B-481B-AF69-28FAF466D2F1"),  # FD Fish Pie
    ("snack", "FB74E794-E7A0-4587-9B12-57C239553B64"),  # granola bars buckwheat
    # Batch 3
    ("main", "065E4E92-601B-4E00-8186-C613ECCC2C88"),  # FD Chicken Green Veg Risotto
    ("soup", "1167B5C0-7F5F-4AE4-A9E1-4077C327D1C0"),  # chicken noodle soup
    # Batch 4
    ("main", "42F32E0E-66B0-4716-BD29-B0EA9E1F93B2"),  # FD Vegetable Korma
    ("snack", "8A8FE02A-4677-4A55-823B-FA8303BA1BFD"),  # Rocher energy balls
    # Batch 5
    ("main", "4370C7A0-FB7E-4A3C-BA46-1C27C3471561"),  # FD Thai Green Chicken Curry
    ("soup", "830FB55B-F0A6-413D-9858-23051A2BAEDA"),  # pumpkin sweet potato soup
    # Batch 6
    ("main", "A72D6AE2-07B2-4873-8AD0-E5FF37E38527"),  # FD Mac + Cheese
    ("main", "D53A7884-BF0F-426C-A83A-6FB36FE2BB5A"),  # Pesto Turkey Meatballs
    # Batch 7
    ("main", "8D5F56EC-9072-4D29-B846-741EE0A2713B"),  # chocolate blueberry clusters (snack type)
    ("soup", "26951C68-2A1F-4ACD-9320-2CAFE66E14F3"),  # Greek Lemon Chicken Soup
    # Batch 8
    ("main", "1524DEAC-32A6-47D3-99CD-757FEA633CDC"),  # pulled pork
    ("soup", "47308319-1209-410F-9493-9B68B06D4690"),  # pumpkin tomato soup
    # Batch 9
    ("main", "20DA0490-06DF-4C2B-A92B-96AC5BE41A9F"),  # fish tacos
    ("snack", "370B6FCD-E8D5-40F7-AEF8-D755BC96F98A"),  # popcorn
    # Batch 10
    ("main", "03630b33-89a9-4d7d-b058-eea780665117"),  # Monash Slow Cooked Lamb
    ("soup", "6566090F-8185-4DEE-A2E3-7AEE39B2B881"),  # vegetable soup
]

# Fix batch 7: first item should be snack type for clusters
PLAN_UIDS[12] = ("snack", "8D5F56EC-9072-4D29-B846-741EE0A2713B")

BATCH_COUNT = 10
MENU_DAYS = 7
BATCH_SCALE = 8  # cook at 8 portions from 1-serve recipes
MENU_NOTES = (
    "Only recipes scored 🟢🟡🟠 (no 🔴). "
    f"Scale each to {BATCH_SCALE} portions (recipes are 1-serve). "
    "Both dishes under Day 1. Lunch + dinner from freezer; breakfast separate."
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def is_our_menu(name: str) -> bool:
    n = (name or "").strip()
    if n.startswith("LF Batch Cook"):
        return True
    if n.startswith("Batch ") and len(n) > 6 and n[6:].split()[0].isdigit():
        return True
    return False


def load_plan() -> list[tuple[str, str, str, int]]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    out = []
    for kind, uid in PLAN_UIDS:
        r = con.execute(
            "select name, servings from recipes where uid=? and coalesce(in_trash,0)=0",
            (uid,),
        ).fetchone()
        if not r:
            raise SystemExit(f"Missing recipe {uid}")
        name = r["name"] or ""
        if name.startswith("🔴"):
            raise SystemExit(f"Recipe is 🔴 (excluded): {name}")
        servings = 1
        try:
            import re

            m = re.search(r"(\d+)", str(r["servings"] or "1"))
            servings = int(m.group(1)) if m else 1
        except Exception:
            servings = 1
        out.append((kind, name, uid, servings))
    con.close()
    return out


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
    plan = load_plan()
    if len(plan) != BATCH_COUNT * 2:
        raise SystemExit(f"Need {BATCH_COUNT * 2} recipes, got {len(plan)}")

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.4)
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
        type_for = {
            "main": types["Dinner"],
            "soup": types["Lunch"],
            "snack": types["Snacks"],
        }

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = body["result"] or []
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        items_all = body["result"] or []

        to_drop_menus = [
            m for m in menus if not m.get("deleted") and is_our_menu(m.get("name") or "")
        ]
        drop_uids = {(m.get("uid") or "").upper() for m in to_drop_menus}
        to_drop_items = [
            it
            for it in items_all
            if (it.get("menu_uid") or "").upper() in drop_uids and not it.get("deleted")
        ]
        safe_print(f"Deleting {len(to_drop_menus)} menus, {len(to_drop_items)} items")
        if to_drop_items:
            await post_entities(
                s,
                limiter,
                H,
                "/v2/sync/menuitems/",
                [{**it, "deleted": True} for it in to_drop_items],
            )
        if to_drop_menus:
            await post_entities(
                s,
                limiter,
                H,
                "/v2/sync/menus/",
                [{**m, "deleted": True} for m in to_drop_menus],
            )

        new_menus: list[dict] = []
        new_items: list[dict] = []
        for batch in range(1, BATCH_COUNT + 1):
            menu_uid = str(uuid.uuid4()).upper()
            pair = plan[(batch - 1) * 2 : batch * 2]
            names = [p[1] for p in pair]
            new_menus.append(
                {
                    "uid": menu_uid,
                    "name": f"Batch {batch}",
                    "notes": MENU_NOTES + f"\n\nRecipes: {names[0]} + {names[1]}",
                    "order_flag": batch,
                    "days": MENU_DAYS,
                }
            )
            for slot, (kind, name, recipe_uid, servings) in enumerate(pair, start=1):
                item: dict = {
                    "uid": str(uuid.uuid4()).upper(),
                    "menu_uid": menu_uid,
                    "recipe_uid": recipe_uid,
                    "name": name,
                    "day": 1,
                    "order_flag": slot,
                    "type_uid": type_for[kind],
                    "is_ingredient": False,
                }
                if servings != BATCH_SCALE:
                    item["scale"] = f"{BATCH_SCALE}/{servings}"
                new_items.append(item)
            safe_print(f"Batch {batch}: {names[0][:50]} | {names[1][:50]}")

        if not await post_entities(s, limiter, H, "/v2/sync/menus/", new_menus):
            raise SystemExit("Failed to create menus")
        chunk = 5
        for i in range(0, len(new_items), chunk):
            part = new_items[i : i + chunk]
            if not await post_entities(s, limiter, H, "/v2/sync/menuitems/", part):
                raise SystemExit(f"Failed menu items chunk {i}")
            safe_print(f"  posted items {i + 1}-{i + len(part)}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        live_menus = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and is_our_menu(m.get("name") or "")
        ]
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        live_items = [
            it
            for it in (body["result"] or [])
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper()
            in {(m.get("uid") or "").upper() for m in live_menus}
        ]
        safe_print(f"Verify menus={len(live_menus)} items={len(live_items)}")
        by_menu: dict[str, list] = {}
        names = {m["uid"].upper(): m["name"] for m in live_menus}
        for it in live_items:
            by_menu.setdefault((it.get("menu_uid") or "").upper(), []).append(it.get("name"))
        for uid, name in sorted(names.items(), key=lambda x: x[1]):
            safe_print(f"  {name}: {by_menu.get(uid, [])}")


if __name__ == "__main__":
    asyncio.run(main())
