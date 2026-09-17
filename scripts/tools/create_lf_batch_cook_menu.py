"""Create Paprika Menu for the 20-day LF batch-cook plan (scale ×8)."""
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
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

# day (1-based), kind, recipe name, recipe uid, recipe servings (for scale)
PLAN: list[tuple[int, str, str, str, int]] = [
    (1, "main", "Field Doctor Chicken Tagine (Low FODMAP)", "7520DE69-2A01-48C7-B1C6-E03AFABEA45D", 8),
    (2, "soup", "Low FODMAP Tomato Basil Soup", "7ECF3B1B-82D7-44CD-97AD-85B9788408CD", 8),
    (3, "main", "Field Doctor Fish Pie (Low FODMAP)", "B7A0C22C-577B-481B-AF69-28FAF466D2F1", 8),
    (4, "snack", "One Bowl Low FODMAP Granola Bars", "7F6F4917-B01F-4B51-A747-F2A3E369054C", 12),
    (5, "main", "Field Doctor Cottage Pie (Low FODMAP)", "68E63E6C-51E1-448C-8CE0-72DC96316691", 8),
    (6, "soup", "Field Doctor Chilli Con Carne (Low FODMAP)", "C261839C-A66A-4AC6-8907-B73ED9526D8A", 8),
    (7, "main", "Field Doctor Vegetable Korma (Low FODMAP)", "42F32E0E-66B0-4716-BD29-B0EA9E1F93B2", 8),
    (8, "snack", "Low FODMAP Rocher Energy balls", "8A8FE02A-4677-4A55-823B-FA8303BA1BFD", 10),
    (9, "main", "Field Doctor Teriyaki Salmon + Whole Grain Rice (Low FODMAP)", "9DC174CD-7A1A-4318-92E6-E9AE17B07C70", 8),
    (10, "soup", "Monash - Roasted Pumpkin & Carrot Soup", "D8D9B135-0E5D-4948-B2A5-5DE04348E674", 6),
    (11, "main", "Field Doctor Thai Green Chicken Curry (Low FODMAP)", "4370C7A0-FB7E-4A3C-BA46-1C27C3471561", 8),
    (12, "main", "Field Doctor Chickpea Masala Curry (Low FODMAP)", "FFFAA87E-280F-45E4-9D43-F1BC3D0FFE0F", 8),
    (13, "main", "Field Doctor Low FODMAP Mac + Cheese (Low FODMAP)", "A72D6AE2-07B2-4873-8AD0-E5FF37E38527", 8),
    (14, "soup", "Low FODMAP chicken noodle soup", "1167B5C0-7F5F-4AE4-A9E1-4077C327D1C0", 6),
    (15, "snack", "Low FODMAP Dark Chocolate Blueberry Mac Nut Clusters", "8D5F56EC-9072-4D29-B846-741EE0A2713B", 8),
    (16, "main", "Field Doctor Smokey Chipotle Meatballs (Low FODMAP)", "A628E586-15D2-4475-A021-C61A657AC5A9", 8),
    (17, "main", "Field Doctor Red Pesto + Roasted Vegetable Penne (Low FODMAP)", "A39B77CB-1723-45D1-B84B-91BE873EA963", 8),
    (18, "soup", "Pumpkin sweet potato soup (low FODMAP)", "830FB55B-F0A6-413D-9858-23051A2BAEDA", 6),
    (19, "main", "Field Doctor Coq au Vin (Low FODMAP)", "CA7A0E87-31B6-4DA8-A80F-E2584EA973DB", 8),
    (20, "main", "Field Doctor Moroccan Chickpea Tagine (Low FODMAP)", "4E19DB85-F6E6-4755-8724-AE8CA9DF0E6C", 8),
]

MENU_NAME = "LF Batch Cook Plan (×8)"
MENU_NOTES = (
    "Cook 1 recipe/day at 8 portions. Lunch + dinner from freezer; breakfast separate. "
    "On snack days, L+D still come from the freezer bank. "
    "Already cooked (not listed): Beef Bolognese + Penne, Potato Topped Chicken Pie."
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def scale_for(base_servings: int) -> str | None:
    if base_servings == 8:
        return None
    return f"8/{base_servings}"


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
        types = {t["name"]: t["uid"] for t in body["result"]}
        type_for = {
            "main": types["Dinner"],
            "soup": types["Lunch"],
            "snack": types["Snacks"],
        }
        safe_print("Meal types:", types)

        menu_uid = str(uuid.uuid4()).upper()
        menu = {
            "uid": menu_uid,
            "name": MENU_NAME,
            "notes": MENU_NOTES,
            "order_flag": 0,
            "days": 20,
        }
        if not await post_entities(s, limiter, H, "/v2/sync/menus/", [menu]):
            raise SystemExit("Failed to create menu")
        safe_print(f"Created menu {menu_uid}")

        items = []
        for day, kind, name, recipe_uid, servings in PLAN:
            items.append(
                {
                    "uid": str(uuid.uuid4()).upper(),
                    "menu_uid": menu_uid,
                    "recipe_uid": recipe_uid,
                    "name": f"Day {day}: {name}",
                    "day": day - 1,  # Paprika uses 0-based day index
                    "order_flag": day,
                    "type_uid": type_for[kind],
                    "scale": scale_for(servings),
                    "is_ingredient": False,
                }
            )

        # Post in one batch
        if not await post_entities(s, limiter, H, "/v2/sync/menuitems/", items):
            raise SystemExit("Failed to create menu items")
        safe_print(f"Created {len(items)} menu items")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        # Verify
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = body["result"]
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        menuitems = body["result"]
        mine = [m for m in menuitems if m.get("menu_uid", "").upper() == menu_uid]
        safe_print(f"Verify menus={len(menus)} items_for_plan={len(mine)}")
        for m in sorted(mine, key=lambda x: x.get("order_flag") or 0)[:5]:
            safe_print(f"  {m.get('name')}")


if __name__ == "__main__":
    asyncio.run(main())
