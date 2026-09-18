"""Review Soups category: keep only titles with soup / broth / chowder (or bisque).

Move everything else to Main Meals, Sides & Salads, or Oils/Sauces as appropriate.

  python scripts/tools/review_soups_category.py
  python scripts/tools/review_soups_category.py --apply
"""
from __future__ import annotations

import argparse
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

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

SOUPS = "Soups"
MAIN = "Main Meals"
SIDES = "Sides & Salads"
OILS = "Oils, Sauces, Dips and Butters"

KEEP_NAME = re.compile(r"\b(soup|soups|broth|chowder|bisque)\b", re.I)

# Condiment / stock → Oils, Sauces…
TO_OILS = re.compile(
    r"(?i)\b(stock|chili\s*oil|chilli\s*oil|infused\s*oil)\b"
)

# Small plates / sides / snacks
TO_SIDES = re.compile(
    r"(?i)("
    r"nuggets?|devill?ed\s*eggs?|aloo\s*tikki|fritters?|"
    r"croquettes?|salad(?!\s*soup)"
    r")"
)


def bare_name(name: str) -> str:
    return re.sub(r"^[🟢🟡🟠🔴]\s*", "", name or "").strip()


def classify(name: str) -> str | None:
    """Return destination category name, or None to keep in Soups."""
    bare = bare_name(name)
    if KEEP_NAME.search(bare):
        return None
    if TO_OILS.search(bare):
        return OILS
    if TO_SIDES.search(bare):
        return SIDES
    return MAIN


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def update_local_categories(recipe_uid: str, category_uids: list[str]) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "DELETE FROM recipes_to_categories WHERE recipe_uid=?", (recipe_uid,)
    )
    for cuid in category_uids:
        con.execute(
            "INSERT OR IGNORE INTO recipes_to_categories (recipe_uid, category_uid) VALUES (?, ?)",
            (recipe_uid, cuid),
        )
    con.execute(
        "UPDATE recipes SET status=? WHERE uid=?",
        ("modified", recipe_uid),
    )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT r.uid, r.name
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE coalesce(r.in_trash, 0) = 0 AND c.name = ?
        ORDER BY r.name COLLATE NOCASE
        """,
        (SOUPS,),
    ).fetchall()
    con.close()

    plan: list[tuple[str, str, str]] = []  # uid, name, dest
    keep = 0
    for r in rows:
        dest = classify(r["name"] or "")
        if dest is None:
            keep += 1
            continue
        plan.append((r["uid"], r["name"] or "", dest))

    by_dest: dict[str, list[str]] = {}
    for _uid, name, dest in plan:
        by_dest.setdefault(dest, []).append(name)

    safe_print(f"Soups total={len(rows)} keep={keep} move={len(plan)}")
    for dest, names in sorted(by_dest.items()):
        safe_print(f"\n→ {dest} ({len(names)})")
        for n in names:
            safe_print(f"  {n}")

    if not args.apply:
        safe_print("\nDry-run only. Pass --apply to update Paprika.")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.3)
    updated = 0
    failed = 0

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=H
        )
        cats = {
            c["name"]: c["uid"]
            for c in (body.get("result") or [])
            if not c.get("deleted")
        }
        soups_uid = cats[SOUPS]
        dest_map = {
            MAIN: cats[MAIN],
            SIDES: cats[SIDES],
            OILS: cats[OILS],
        }
        soups_l = soups_uid.lower()

        for uid, name, dest in plan:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                failed += 1
                safe_print(f"FAIL missing {name}")
                continue

            old_cats = [str(c) for c in (rec.get("categories") or [])]
            new_cats = [c for c in old_cats if c.lower() != soups_l]
            dest_uid = dest_map[dest]
            if dest_uid.lower() not in {c.lower() for c in new_cats}:
                new_cats.append(dest_uid)
            if set(x.lower() for x in new_cats) == set(x.lower() for x in old_cats):
                safe_print(f"skip (already) {name}")
                continue

            rec["categories"] = new_cats
            rec["hash"] = calc_hash(rec)

            await limiter.wait_turn()
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H, data=form
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            if ok:
                updated += 1
                update_local_categories(uid, new_cats)
                safe_print(f"moved → {dest}: {name}")
            else:
                failed += 1
                safe_print(f"FAIL save {name}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            await r.text()

    safe_print(f"\nDone updated={updated} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
