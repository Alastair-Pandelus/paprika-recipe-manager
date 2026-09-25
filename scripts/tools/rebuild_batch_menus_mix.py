"""Rebuild Batch menus: 60% mains / 20% soups / 10% bars / 10% baking, non-❌/🔴, scale 8.

  python scripts/tools/rebuild_batch_menus_mix.py
  python scripts/tools/rebuild_batch_menus_mix.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
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

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

BATCH_COUNT = 10
MENU_DAYS = 7
BATCH_SCALE = 8
# Across 20 slots: 12 main (60%), 4 soup (20%), 2 bar (10%), 2 baking (10%)
# Day pattern: 1–4 main+soup, 5–6 main+bar, 7–8 main+baking, 9–10 main+main
BATCH_KINDS: list[tuple[str, str]] = [
    ("main", "soup"),
    ("main", "soup"),
    ("main", "soup"),
    ("main", "soup"),
    ("main", "bar"),
    ("main", "bar"),
    ("main", "baking"),
    ("main", "baking"),
    ("main", "main"),
    ("main", "main"),
]

CAT = {
    "main": "Main Meals",
    "soup": "Soups",
    "bar": "Snacks & Bars",
    "baking": "Baking & Sweet Treats",
}

MENU_NOTES = (
    "Low FODMAP batch cook (✅ℹ️ only — no ❌). "
    "Mix ~60% mains / 20% soups / 10% bars / 10% baking. "
    f"Scale each recipe to {BATCH_SCALE} portions (recipes are 1-serve). "
    "Both dishes under Day 1. Lunch + dinner from freezer; breakfast separate."
)


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def is_our_menu(name: str) -> bool:
    n = (name or "").strip()
    if n == "Batch":
        return True
    if n.startswith("LF Batch Cook"):
        return True
    if n.startswith("Batch ") and len(n) > 6 and n[6:].split()[0].isdigit():
        return True
    return False


def emoji_rank(name: str) -> int:
    if name.startswith(("✅", "🟢")):
        return 0
    if name.startswith(("⚠️", "ℹ️", "🟡", "🟠")):
        return 1
    return 9


def parse_servings(raw) -> int:
    try:
        m = re.search(r"(\d+)", str(raw or "1"))
        return int(m.group(1)) if m else 1
    except Exception:
        return 1


def pool_for(kind: str) -> list[dict]:
    cat = CAT[kind]
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT r.uid, r.name, r.servings
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE coalesce(r.in_trash, 0) = 0
          AND c.name = ?
          AND (
            r.name GLOB '[🟢🟡🟠]*'
            OR r.name LIKE '✅%'
            OR r.name LIKE 'ℹ️%'
            OR r.name LIKE '⚠️%'
          )
        ORDER BY r.name COLLATE NOCASE
        """,
        (cat,),
    ).fetchall()
    con.close()
    out = []
    seen = set()
    for r in rows:
        uid = (r["uid"] or "").upper()
        if uid in seen:
            continue
        name = r["name"] or ""
        low = name.lower()
        # Tighten name heuristics — Paprika categories are sometimes wrong
        if kind == "soup" and not re.search(
            r"\b(soup|chowder|bisque|broth)\b", low
        ):
            continue
        if kind == "bar":
            if not re.search(r"\b(bar|bars|bark)\b", low):
                continue
            if re.search(r"\b(sauce|dressing|marinade|stock)\b", low):
                continue
        if kind == "baking":
            if re.search(r"\b(bar|bars)\b", low) and "bark" not in low:
                # prefer non-bar baking (cakes, muffins, cookies, breads)
                if not re.search(
                    r"\b(muffin|cookie|brownie|cake|bread|biscuit|scone|shortbread)\b",
                    low,
                ):
                    continue
            if re.search(r"\b(sauce|dressing|marinade|stock|soup)\b", low):
                continue
        if kind == "main" and re.search(
            r"\b(soup|chowder|muffin|cookie|bark|popcorn|energy ball)\b", low
        ):
            continue
        seen.add(uid)
        out.append(
            {
                "uid": r["uid"],
                "name": name,
                "servings": parse_servings(r["servings"]),
                "kind": kind,
            }
        )
    # Prefer greener scores, then shuffle within rank for variety
    random.seed(42)
    by_rank: dict[int, list] = {}
    for rec in out:
        by_rank.setdefault(emoji_rank(rec["name"]), []).append(rec)
    ordered = []
    for rank in sorted(by_rank):
        group = by_rank[rank]
        random.shuffle(group)
        ordered.extend(group)
    return ordered


def build_plan() -> list[tuple[str, str, str, int]]:
    pools = {k: pool_for(k) for k in ("main", "soup", "bar", "baking")}
    need = {"main": 12, "soup": 4, "bar": 2, "baking": 2}
    for k, n in need.items():
        if len(pools[k]) < n:
            raise SystemExit(
                f"Need {n} {k} recipes, only found {len(pools[k])} non-red in {CAT[k]}"
            )

    used: set[str] = set()
    picks: dict[str, list[dict]] = {k: [] for k in need}

    def take(kind: str, n: int) -> None:
        for rec in pools[kind]:
            if len(picks[kind]) >= n:
                break
            uid = rec["uid"].upper()
            if uid in used:
                continue
            used.add(uid)
            picks[kind].append(rec)

    for k, n in need.items():
        take(k, n)

    plan: list[tuple[str, str, str, int]] = []
    idx = {k: 0 for k in need}
    for a, b in BATCH_KINDS:
        for kind in (a, b):
            i = idx[kind]
            rec = picks[kind][i]
            idx[kind] = i + 1
            plan.append((kind, rec["name"], rec["uid"], rec["servings"]))
    return plan


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

    plan = build_plan()
    kinds = [p[0] for p in plan]
    safe_print(
        f"Plan {len(plan)} slots | "
        f"main={kinds.count('main')} soup={kinds.count('soup')} "
        f"bar={kinds.count('bar')} baking={kinds.count('baking')} "
        f"| scale={BATCH_SCALE}"
    )
    for batch in range(1, BATCH_COUNT + 1):
        pair = plan[(batch - 1) * 2 : batch * 2]
        safe_print(
            f"Batch {batch}: [{pair[0][0]}] {pair[0][1][:48]}  |  [{pair[1][0]}] {pair[1][1][:48]}"
        )

    if not args.apply:
        safe_print("Dry-run only. Pass --apply to write menus.")
        return

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
            by_menu.setdefault((it.get("menu_uid") or "").upper(), []).append(
                f"{it.get('name')} (scale={it.get('scale')})"
            )
        for uid, name in sorted(names.items(), key=lambda x: x[1]):
            safe_print(f"  {name}: {by_menu.get(uid, [])}")


if __name__ == "__main__":
    asyncio.run(main())
