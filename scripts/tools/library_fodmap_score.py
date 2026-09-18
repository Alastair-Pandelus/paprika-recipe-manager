"""Apply FODMAP proxy scores across the Paprika library.

  python scripts/tools/library_fodmap_score.py
  python scripts/tools/library_fodmap_score.py --apply
  python scripts/tools/library_fodmap_score.py --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fodmap_score_lib import transform_recipe  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OUT_DIR = Path(__file__).resolve().parent / ".library_fodmap_score_dryrun"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def list_recipes(limit: int = 0, name_substr: str = "") -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    if name_substr:
        rows = con.execute(
            """
            SELECT uid, name, ingredients, description, notes, servings
            FROM recipes
            WHERE coalesce(in_trash, 0) = 0
              AND name LIKE ?
            ORDER BY name COLLATE NOCASE
            """,
            (f"%{name_substr}%",),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT uid, name, ingredients, description, notes, servings
            FROM recipes
            WHERE coalesce(in_trash, 0) = 0
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()
    con.close()
    out = [dict(r) for r in rows]
    if limit > 0:
        out = out[:limit]
    return out


async def post_recipe(session, headers, recipe: dict, limiter: RateLimiter) -> bool:
    await limiter.wait_turn()
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(recipe),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
        headers=headers,
        data=form,
    ) as r:
        return '"result":true' in (await r.text()).replace(" ", "")


def update_local(uid: str, t: dict) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        """
        UPDATE recipes
        SET name=?, ingredients=?, description=?, notes=?, status=?
        WHERE uid=?
        """,
        (
            t["name"],
            t["ingredients"],
            t["description"],
            t["notes"],
            "modified",
            uid,
        ),
    )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--name", type=str, default="", help="Substring filter on recipe name")
    args = ap.parse_args()
    apply = bool(args.apply)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recipes = list_recipes(args.limit, args.name)
    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"Library FODMAP score | {mode} | {len(recipes)} recipes")

    results = []
    changed = 0
    updated = 0
    limiter = RateLimiter(0.28)
    headers = None

    if apply:
        user, pw = paprika_credentials()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        if apply:
            st, body = await api_json(
                session,
                limiter,
                "POST",
                f"{PAPRIKA_API}/v1/account/login",
                data={"email": user, "password": pw},
            )
            if st != 200:
                raise SystemExit(f"login failed {st}")
            headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for i, local in enumerate(recipes, 1):
            t = transform_recipe(
                local["name"] or "",
                local["ingredients"] or "",
                local["description"] or "",
                local["notes"] or "",
            )
            did = (
                t["name"] != (local["name"] or "")
                or t["ingredients"] != (local["ingredients"] or "")
                or t["description"] != (local["description"] or "")
                or t["notes"] != (local["notes"] or "")
            )
            if did:
                changed += 1
            results.append(
                {
                    "uid": local["uid"],
                    "name": t["name"],
                    "meal_emoji": t["meal_emoji"],
                    "stacks": t["stacks"],
                    "changed": did,
                }
            )

            if apply and did:
                assert headers is not None
                st, body = await api_json(
                    session,
                    limiter,
                    "GET",
                    f"{PAPRIKA_API}/v2/sync/recipe/{local['uid']}/",
                    headers=headers,
                )
                rec = (body or {}).get("result") or {}
                if not rec.get("uid"):
                    safe_print(f"FAIL missing {local['name']}")
                    continue
                # Re-transform from cloud text in case local drifted
                t2 = transform_recipe(
                    rec.get("name") or "",
                    rec.get("ingredients") or "",
                    rec.get("description") or "",
                    rec.get("notes") or "",
                )
                rec["name"] = t2["name"]
                rec["ingredients"] = t2["ingredients"]
                rec["description"] = t2["description"]
                rec["notes"] = t2["notes"]
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(session, headers, rec, limiter)
                if ok:
                    updated += 1
                    update_local(local["uid"], t2)
                else:
                    safe_print(f"FAIL save {t2['name']}")

            if i % 50 == 0 or i == len(recipes):
                safe_print(
                    f"progress {i}/{len(recipes)} changed={changed} updated={updated}"
                )

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    summary = {
        "mode": mode,
        "total": len(recipes),
        "changed": changed,
        "updated": updated if apply else 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recipes": results,
    }
    (OUT_DIR / "_index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    safe_print(
        f"Done | total={len(recipes)} changed={changed} updated={updated if apply else 0}"
    )


if __name__ == "__main__":
    asyncio.run(main())
