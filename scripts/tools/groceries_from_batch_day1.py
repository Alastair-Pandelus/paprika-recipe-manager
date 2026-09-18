"""Clear Paprika groceries and fill with Batch Day 1 recipes scaled to 8 portions.

  python scripts/tools/groceries_from_batch_day1.py
  python scripts/tools/groceries_from_batch_day1.py --apply
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

from fodmap_score_lib import parse_line, strip_inline_fodmap  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from rebuild_single_batch_menu import MENU_NAME  # noqa: E402
from remediate_red_fodmap_recipe import rewrite_qty_prefix  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
SCALE = 8


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def clean_ingredient_line(line: str) -> str:
    s = strip_inline_fodmap(line)
    s = re.sub(r"\[recipe:([^\]]+)\]", r"\1", s)
    s = re.sub(r"\s*//.*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def scale_line(line: str, factor: float = SCALE) -> str | None:
    """Return scaled ingredient text, or None to skip empty/header lines."""
    s = clean_ingredient_line(line)
    if not s or s.startswith("**") or s.lower().startswith("note"):
        return None
    p = parse_line(s)
    if not p:
        return s  # salt / garnish / unparsed — keep as-is
    # Multiply quantity
    new_val = p.value * factor
    return rewrite_qty_prefix(s, new_val, p.unit, p.rest, floor=False)


def split_qty_name(scaled: str) -> tuple[str | None, str]:
    """Best-effort split into quantity + ingredient for Paprika grocery fields."""
    p = parse_line(scaled)
    if not p:
        return None, scaled
    # Rebuild qty token from start of line
    m = re.match(
        r"^((?:pinch of\s+)|(?:\d+\s*/\s*\d+(?:\s+to\s+\d+\s*/\s*\d+)?|\d+\s+\d+\s*/\s*\d+|\d+(?:\.\d+)?(?:\s+to\s+\d+(?:\.\d+)?)?)\s*(?:x\s+400\s*g\s*tins?)?(?:\s*(?:g|kg|ml|l|tsp|tbsp|cup|cups|medium|large|small))?)\s+(.+)$",
        scaled,
        re.I,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # fallback: use rewrite pieces
    from remediate_red_fodmap_recipe import format_kitchen_qty

    qty = format_kitchen_qty(p.value, p.unit, floor=False)
    return qty, p.rest


def recipe_ingredients(uid: str) -> tuple[str, str]:
    con = sqlite3.connect(LOCAL_DB)
    row = con.execute(
        "SELECT name, ingredients FROM recipes WHERE uid=?", (uid,)
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"Recipe not found {uid}")
    return row[0] or "", row[1] or ""


async def post_groceries(session, headers, limiter, items: list) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(items),
        content_type="application/octet-stream",
        filename="data",
    )
    await limiter.wait_turn()
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/groceries/", headers=headers, data=form
    ) as r:
        txt = await r.text()
        ok = '"result":true' in txt.replace(" ", "")
        if not ok:
            safe_print(f"POST groceries failed: {r.status} {txt[:400]}")
        return ok


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)

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
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/grocerylists/", headers=H
        )
        lists = [x for x in (body["result"] or []) if not x.get("deleted")]
        default = next((x for x in lists if x.get("is_default")), lists[0] if lists else None)
        list_uid = default["uid"] if default else None

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menus/", headers=H
        )
        menus = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and (m.get("name") or "").strip() == MENU_NAME
        ]
        if not menus:
            raise SystemExit("Batch menu not found")
        menu_uid = (menus[0].get("uid") or "").upper()

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/menuitems/", headers=H
        )
        day1 = [
            it
            for it in (body["result"] or [])
            if not it.get("deleted")
            and (it.get("menu_uid") or "").upper() == menu_uid
            and int(it.get("day") or 0) == 1
        ]
        day1.sort(key=lambda it: it.get("order_flag") or 0)
        if len(day1) < 2:
            raise SystemExit(f"Expected 2 Day-1 items, found {len(day1)}")

        grocery_rows: list[dict] = []
        order = 0
        for it in day1:
            ruid = it.get("recipe_uid") or ""
            rname, ings = recipe_ingredients(ruid)
            # Prefer live menu name (with emoji) for recipe field display
            recipe_label = re.sub(
                r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*", "", it.get("name") or rname
            )
            safe_print(f"\n{it.get('name')} ×{SCALE}")
            for line in ings.splitlines():
                scaled = scale_line(line, SCALE)
                if not scaled:
                    continue
                qty, ing = split_qty_name(scaled)
                name = f"{qty} {ing}".strip() if qty else ing
                safe_print(f"  {name}")
                grocery_rows.append(
                    {
                        "uid": str(uuid.uuid4()).upper(),
                        "name": name,
                        "ingredient": ing,
                        "quantity": qty,
                        "aisle": "Other",
                        "aisle_uid": None,
                        "purchased": False,
                        "recipe": recipe_label,
                        "recipe_uid": ruid,
                        "instruction": "",
                        "separate": False,
                        "list_uid": list_uid,
                        "order_flag": order,
                    }
                )
                order += 1

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/groceries/", headers=H
        )
        existing = [g for g in (body["result"] or []) if not g.get("deleted")]
        safe_print(f"\nClear {len(existing)} existing → add {len(grocery_rows)} new")

        if not apply:
            safe_print("Dry-run only. Pass --apply to write.")
            return

        if existing:
            deleted = [{**g, "deleted": True, "purchased": False} for g in existing]
            # chunk deletes
            for i in range(0, len(deleted), 20):
                if not await post_groceries(s, H, limiter, deleted[i : i + 20]):
                    raise SystemExit("delete failed")
            safe_print(f"Deleted {len(deleted)}")

        for i in range(0, len(grocery_rows), 20):
            if not await post_groceries(s, H, limiter, grocery_rows[i : i + 20]):
                raise SystemExit("add failed")
        safe_print(f"Added {len(grocery_rows)}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/groceries/", headers=H
        )
        active = [g for g in (body["result"] or []) if not g.get("deleted")]
        safe_print(f"Done | active groceries={len(active)}")


if __name__ == "__main__":
    asyncio.run(main())
