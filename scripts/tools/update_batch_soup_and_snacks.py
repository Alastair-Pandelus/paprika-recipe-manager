"""On Batch menu: swap Day-2 soup to Pumpkin sweet potato; replace snacks with bars/baking."""
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

PUMPKIN_UID = "830FB55B-F0A6-413D-9858-23051A2BAEDA"

# Prefer these bar/baking titles (substring match), then fill from category
PREFERRED = [
    "lemon bar",
    "cheesecake bars",
    "cookie dough bars",
    "pecan bars",
    "s'mores bars",
    "chocolate bark",
    "chocolate chip cookies",
    "blueberry muffins",
    "banana muffins",
    "brownies",
    "pumpkin bread",
    "banana bread",
]


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def load_recipe(uid: str) -> dict:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT uid, name, servings FROM recipes WHERE uid=? AND coalesce(in_trash,0)=0",
        (uid,),
    ).fetchone()
    con.close()
    if not r:
        raise SystemExit(f"Recipe missing {uid}")
    return {
        "uid": r["uid"],
        "name": r["name"],
        "servings": parse_servings(r["servings"]),
    }


def pick_bars_baking(n: int, exclude: set[str]) -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT DISTINCT r.uid, r.name, r.servings
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE coalesce(r.in_trash, 0) = 0
          AND (r.name LIKE '✅%' OR r.name LIKE '⚠️%')
          AND c.name IN ('Snacks & Bars', 'Baking & Sweet Treats')
          AND (
            lower(r.name) LIKE '%bar%'
            OR lower(r.name) LIKE '%muffin%'
            OR lower(r.name) LIKE '%cookie%'
            OR lower(r.name) LIKE '%brownie%'
            OR lower(r.name) LIKE '%bread%'
            OR lower(r.name) LIKE '%bark%'
            OR c.name = 'Baking & Sweet Treats'
          )
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

    picked: list[dict] = []
    used: set[str] = set()
    for pref in PREFERRED:
        if len(picked) >= n:
            break
        for rec in pool:
            uid = rec["uid"].upper()
            if uid in used:
                continue
            if pref in rec["name"].lower():
                picked.append(rec)
                used.add(uid)
                break

    # Prefer ✅ bars / muffins / cookies to fill
    def score(rec: dict) -> tuple:
        name = rec["name"].lower()
        is_ok = 0 if rec["name"].startswith("✅") else 1
        kind = 0 if any(k in name for k in ("bar", "muffin", "cookie", "brownie", "bark", "bread")) else 2
        return (is_ok, kind, rec["name"])

    for rec in sorted(pool, key=score):
        if len(picked) >= n:
            break
        uid = rec["uid"].upper()
        if uid in used:
            continue
        picked.append(rec)
        used.add(uid)

    if len(picked) < n:
        raise SystemExit(f"Need {n} bars/baking, found {len(picked)}")
    return picked


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
            safe_print(f"POST failed {r.status} {txt[:300]}")
        return ok


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
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/mealtypes/", headers=H
        )
        types = {t["name"]: t["uid"] for t in body["result"] if not t.get("deleted")}
        snacks_uid = (types.get("Snacks") or "").upper()
        lunch_uid = (types.get("Lunch") or "").upper()

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

        # Soup to replace: corn chowder / kale / day-2 lunch that isn't pumpkin
        soup_targets = [
            it
            for it in items
            if (it.get("type_uid") or "").upper() == lunch_uid
            and "pumpkin sweet potato" not in (it.get("name") or "").lower()
            and (
                "corn chowder" in (it.get("name") or "").lower()
                or "kale" in (it.get("name") or "").lower()
                or int(it.get("day") or 0) == 2
            )
        ]
        # Prefer day 2 lunch specifically
        day2_lunch = [
            it
            for it in items
            if int(it.get("day") or 0) == 2
            and (it.get("type_uid") or "").upper() == lunch_uid
        ]
        if day2_lunch:
            soup_targets = day2_lunch

        snack_items = [
            it
            for it in items
            if (it.get("type_uid") or "").upper() == snacks_uid
        ]
        snack_items.sort(key=lambda it: (int(it.get("day") or 0), it.get("order_flag") or 0))

        pumpkin = load_recipe(PUMPKIN_UID)
        used = {(it.get("recipe_uid") or "").upper() for it in items}
        used.discard(PUMPKIN_UID.upper())
        for it in soup_targets + snack_items:
            used.discard((it.get("recipe_uid") or "").upper())

        bars = pick_bars_baking(len(snack_items), used)

        safe_print("Soup swap:")
        for it in soup_targets:
            safe_print(f"  {it.get('name')} → {pumpkin['name']}")
        safe_print("Snack → bars/baking:")
        for it, rec in zip(snack_items, bars):
            safe_print(f"  Day {it.get('day')}: {it.get('name')} → {rec['name']}")

        if not apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        updated = []
        for it in soup_targets:
            updated.append(
                {
                    **it,
                    "recipe_uid": pumpkin["uid"],
                    "name": pumpkin["name"],
                    "scale": f"{BATCH_SCALE}/{pumpkin['servings']}",
                }
            )
        for it, rec in zip(snack_items, bars):
            updated.append(
                {
                    **it,
                    "recipe_uid": rec["uid"],
                    "name": rec["name"],
                    "scale": f"{BATCH_SCALE}/{rec['servings']}",
                }
            )

        if not await post_entities(s, limiter, H, "/v2/sync/menuitems/", updated):
            raise SystemExit("update failed")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
        safe_print(f"Done | updated={len(updated)}")


if __name__ == "__main__":
    asyncio.run(main())
