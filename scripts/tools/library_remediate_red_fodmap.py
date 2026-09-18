"""Remediate all 🔴-titled Paprika recipes toward ≤🟠 by scaling FODMAP drivers.

  python scripts/tools/library_remediate_red_fodmap.py
  python scripts/tools/library_remediate_red_fodmap.py --apply
  python scripts/tools/library_remediate_red_fodmap.py --limit 20
  python scripts/tools/library_remediate_red_fodmap.py --target 1.4 --apply
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
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fodmap_score_lib import meal_score_emoji, transform_recipe  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from remediate_red_fodmap_recipe import diagnose, remediate_ingredients  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OUT_DIR = Path(__file__).resolve().parent / ".library_remediate_red_dryrun"
_TITLE_EMOJI_RE = re.compile(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*")


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def list_red_recipes(limit: int = 0) -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, description, notes
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
          AND (name LIKE '🔴%' OR name LIKE '❌%')
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


def plan_remediate(local: dict, target: float) -> dict | None:
    """Return transform dict if remediable to non-red; else None with reason in caller."""
    rows, stacks = diagnose(local.get("ingredients") or "")
    before = meal_score_emoji(stacks)
    if before != "❌" and max(stacks.values(), default=0) <= target:
        # Title says red but stacks are fine — just re-annotate
        base = _TITLE_EMOJI_RE.sub("", local.get("name") or "")
        return transform_recipe(
            base,
            local.get("ingredients") or "",
            local.get("description") or "",
            local.get("notes") or "",
        )

    cut, meta = remediate_ingredients(local.get("ingredients") or "", target=target)
    base = _TITLE_EMOJI_RE.sub("", local.get("name") or "")
    t = transform_recipe(
        base, cut, local.get("description") or "", local.get("notes") or ""
    )
    t["_meta"] = meta
    t["_before"] = before
    return t


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=float, default=1.4)
    args = ap.parse_args()
    apply = bool(args.apply)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recipes = list_red_recipes(args.limit)
    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(
        f"Library remediate red FODMAP | {mode} | {len(recipes)} recipes | target={args.target}"
    )

    results = []
    fixed = 0
    still_red = 0
    skipped = 0
    updated = 0
    failed = 0
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
            t = plan_remediate(local, args.target)
            if t is None:
                skipped += 1
                results.append(
                    {
                        "uid": local["uid"],
                        "name": local["name"],
                        "status": "skipped",
                    }
                )
                continue

            after = t["meal_emoji"]
            if after == "❌":
                still_red += 1
                results.append(
                    {
                        "uid": local["uid"],
                        "name": local["name"],
                        "status": "still_red",
                        "stacks": t.get("stacks"),
                    }
                )
            else:
                fixed += 1
                results.append(
                    {
                        "uid": local["uid"],
                        "name": t["name"],
                        "status": "fixed",
                        "meal_emoji": after,
                        "stacks": t.get("stacks"),
                        "changes": len((t.get("_meta") or {}).get("changes") or []),
                    }
                )

            if apply and after != "❌":
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
                    safe_print(f"FAIL missing {local['name']}")
                    continue

                cut, _ = remediate_ingredients(
                    rec.get("ingredients") or "", target=args.target
                )
                # If title was red but stacks already OK, cut may be unchanged
                _, cloud_stacks = diagnose(rec.get("ingredients") or "")
                if meal_score_emoji(cloud_stacks) != "❌" and max(
                    cloud_stacks.values(), default=0
                ) <= args.target:
                    cut = rec.get("ingredients") or ""

                t2 = transform_recipe(
                    _TITLE_EMOJI_RE.sub("", rec.get("name") or ""),
                    cut,
                    rec.get("description") or "",
                    rec.get("notes") or "",
                )
                if t2["meal_emoji"] == "❌":
                    still_red += 1
                    fixed = max(0, fixed - 1)
                    safe_print(f"SKIP still red cloud {local['name']}")
                    continue

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
                    safe_print(f"FAIL save {t2['name']}")

            if i % 25 == 0 or i == len(recipes):
                safe_print(
                    f"progress {i}/{len(recipes)} fixed={fixed} still_red={still_red} "
                    f"updated={updated} failed={failed}"
                )

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    summary = {
        "mode": mode,
        "target": args.target,
        "total": len(recipes),
        "fixed": fixed,
        "still_red": still_red,
        "skipped": skipped,
        "updated": updated if apply else 0,
        "failed": failed,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recipes": results,
    }
    (OUT_DIR / "_index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    safe_print(
        f"Done | total={len(recipes)} fixed={fixed} still_red={still_red} "
        f"updated={updated if apply else 0} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
