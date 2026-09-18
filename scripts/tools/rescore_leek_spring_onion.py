"""Rescore recipes that mention leek or spring/scallion/green onion."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fodmap_score_lib import reload_foods, transform_recipe  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def list_recipes() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, description, notes
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
          AND (
            ingredients LIKE '%leek%'
            OR ingredients LIKE '%spring onion%'
            OR ingredients LIKE '%scallion%'
            OR ingredients LIKE '%green onion%'
          )
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


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
        "UPDATE recipes SET name=?, ingredients=?, description=?, notes=?, status=? WHERE uid=?",
        (t["name"], t["ingredients"], t["description"], t["notes"], "modified", uid),
    )
    con.commit()
    con.close()


async def main() -> None:
    apply = "--apply" in sys.argv
    reload_foods()
    recipes = list_recipes()
    safe_print(f"Rescore leek/spring onion | {'APPLY' if apply else 'DRY-RUN'} | {len(recipes)}")

    changed = updated = failed = 0
    samples = []
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
                if len(samples) < 15:
                    samples.append(f"{t['name'][:50]} stacks={t['stacks']}")

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
                    failed += 1
                    continue
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
                    failed += 1

            if i % 50 == 0 or i == len(recipes):
                safe_print(f"progress {i}/{len(recipes)} changed={changed} updated={updated}")

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    for s in samples:
        safe_print(f"  {s}")
    safe_print(f"Done | changed={changed} updated={updated} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
