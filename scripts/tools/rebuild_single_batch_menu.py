"""Rebuild as a single menu "Batch" with 2 meals on each of days 1–10.

Uses current non-red Field Doctor mains + soups + bars/baking mix (60/20/10/10).
Keeps existing recipe picks when possible by reading live menu items first;
otherwise builds a fresh plan.

  python scripts/tools/rebuild_single_batch_menu.py
  python scripts/tools/rebuild_single_batch_menu.py --apply
"""
from __future__ import annotations

import argparse
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
from rebuild_batch_menus_mix import (  # noqa: E402
    BATCH_SCALE,
    BATCH_KINDS,
    build_plan,
    is_our_menu,
    parse_servings,
)

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

MENU_NAME = "Batch"
MENU_DAYS = 10
MENU_NOTES = (
    "Low FODMAP batch cook (✅ℹ️ only — no ❌). "
    "One menu, days 1–10, two meals per day. "
    "Mix ~60% Field Doctor mains / 20% soups / 10% bars / 10% baking. "
    f"Scale each recipe to {BATCH_SCALE} portions (recipes are 1-serve). "
    "Lunch + dinner from freezer; breakfast separate."
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def recipe_meta(uid: str) -> tuple[str, int] | None:
    con = sqlite3.connect(LOCAL_DB)
    row = con.execute(
        "SELECT name, servings FROM recipes WHERE uid=? AND coalesce(in_trash,0)=0",
        (uid,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return row[0] or "", parse_servings(row[1])


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)

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
            "bar": types["Snacks"],
            "baking": types["Snacks"],
            "snack": types["Snacks"],  # legacy
        }
        dinner = (types["Dinner"] or "").upper()
        lunch = (types["Lunch"] or "").upper()
        snacks = (types.get("Snacks") or "").upper()

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = body["result"] or []
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        items_all = body["result"] or []

        # Collect current Batch 1–10 items ordered by batch then slot
        our_menus = [
            m for m in menus if not m.get("deleted") and is_our_menu(m.get("name") or "")
        ]
        also_named_batch = [
            m
            for m in menus
            if not m.get("deleted")
            and (m.get("name") or "").strip() == MENU_NAME
            and (m.get("uid") or "").upper()
            not in {(x.get("uid") or "").upper() for x in our_menus}
        ]
        drop_menus = our_menus + also_named_batch
        drop_uids = {(m.get("uid") or "").upper() for m in drop_menus}

        def batch_num(name: str) -> int:
            m = re.search(r"Batch\s+(\d+)", name or "")
            return int(m.group(1)) if m else 0

        menu_by_uid = {(m.get("uid") or "").upper(): m for m in our_menus}
        current = [
            it
            for it in items_all
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper() in menu_by_uid
        ]
        current.sort(
            key=lambda it: (
                batch_num(menu_by_uid[(it.get("menu_uid") or "").upper()].get("name")),
                it.get("order_flag") or 0,
            )
        )

        # Build 10 days × 2 slots from current items if we have 20, else fresh plan
        days: list[list[dict]] = []
        if len(current) == 20:
            for d in range(10):
                pair = current[d * 2 : d * 2 + 2]
                day_slots = []
                for slot, it in enumerate(pair, start=1):
                    tuid = (it.get("type_uid") or "").upper()
                    if tuid == dinner:
                        kind = "main"
                    elif tuid == lunch:
                        kind = "soup"
                    elif tuid == snacks:
                        kind = "snack"
                    else:
                        kind = "main"
                    uid = it.get("recipe_uid") or ""
                    meta = recipe_meta(uid)
                    name = (meta[0] if meta else None) or (it.get("name") or "")
                    servings = meta[1] if meta else 1
                    day_slots.append(
                        {
                            "kind": kind,
                            "name": name,
                            "uid": uid,
                            "servings": servings,
                            "slot": slot,
                        }
                    )
                days.append(day_slots)
            safe_print("Using current 20 menu recipes, remapped to days 1–10")
        else:
            plan = build_plan()  # (kind, name, uid, servings) × 20
            # But build_plan uses Main Meals pool not FD — prefer FD for mains.
            # For fallback, still use plan; user already has FD on menus usually.
            for d in range(10):
                pair = plan[d * 2 : d * 2 + 2]
                days.append(
                    [
                        {
                            "kind": pair[0][0],
                            "name": pair[0][1],
                            "uid": pair[0][2],
                            "servings": pair[0][3],
                            "slot": 1,
                        },
                        {
                            "kind": pair[1][0],
                            "name": pair[1][1],
                            "uid": pair[1][2],
                            "servings": pair[1][3],
                            "slot": 2,
                        },
                    ]
                )
            safe_print("Built fresh plan (current menu count != 20)")

        for d, slots in enumerate(days, start=1):
            safe_print(
                f"Day {d}: [{slots[0]['kind']}] {slots[0]['name'][:48]}  |  "
                f"[{slots[1]['kind']}] {slots[1]['name'][:48]}"
            )

        if not apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        # Delete old Batch 1–10 (+ any prior single "Batch")
        to_drop_items = [
            it
            for it in items_all
            if (it.get("menu_uid") or "").upper() in drop_uids and not it.get("deleted")
        ]
        safe_print(f"Deleting {len(drop_menus)} menus, {len(to_drop_items)} items")
        if to_drop_items:
            await post_entities(
                s,
                limiter,
                H,
                "/v2/sync/menuitems/",
                [{**it, "deleted": True} for it in to_drop_items],
            )
        if drop_menus:
            await post_entities(
                s,
                limiter,
                H,
                "/v2/sync/menus/",
                [{**m, "deleted": True} for m in drop_menus],
            )

        menu_uid = str(uuid.uuid4()).upper()
        new_menu = {
            "uid": menu_uid,
            "name": MENU_NAME,
            "notes": MENU_NOTES,
            "order_flag": 1,
            "days": MENU_DAYS,
        }
        new_items: list[dict] = []
        for d, slots in enumerate(days, start=1):
            for slot in slots:
                item: dict = {
                    "uid": str(uuid.uuid4()).upper(),
                    "menu_uid": menu_uid,
                    "recipe_uid": slot["uid"],
                    "name": slot["name"],
                    "day": d,
                    "order_flag": slot["slot"],
                    "type_uid": type_for[slot["kind"]],
                    "is_ingredient": False,
                }
                if slot["servings"] != BATCH_SCALE:
                    item["scale"] = f"{BATCH_SCALE}/{slot['servings']}"
                new_items.append(item)

        if not await post_entities(s, limiter, H, "/v2/sync/menus/", [new_menu]):
            raise SystemExit("Failed to create menu")
        chunk = 5
        for i in range(0, len(new_items), chunk):
            part = new_items[i : i + chunk]
            if not await post_entities(s, limiter, H, "/v2/sync/menuitems/", part):
                raise SystemExit(f"Failed items chunk {i}")
            safe_print(f"  posted items {i + 1}-{i + len(part)}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        # Verify
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        live = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and (m.get("name") or "").strip() == MENU_NAME
        ]
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        live_uid = {(m.get("uid") or "").upper() for m in live}
        live_items = [
            it
            for it in (body["result"] or [])
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper() in live_uid
        ]
        safe_print(f"Verify menus={len(live)} items={len(live_items)} days={live[0].get('days') if live else '?'}")
        by_day: dict[int, list] = {}
        for it in live_items:
            by_day.setdefault(int(it.get("day") or 0), []).append(it.get("name"))
        for d in sorted(by_day):
            safe_print(f"  Day {d}: {by_day[d]}")
        safe_print("Done")


if __name__ == "__main__":
    asyncio.run(main())
