"""Remove plain garlic-infused oil lines when a [recipe:…] link already exists.

Keeps the linked line (e.g. [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]).
Drops the plain duplicate (garlic-infused olive oil / garlic infused oil).

  python scripts/tools/fix_garlic_oil_dupes.py
  python scripts/tools/fix_garlic_oil_dupes.py --apply
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

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OUT_DIR = Path(__file__).resolve().parent / ".fix_garlic_oil_dupes_dryrun"

LINK_RE = re.compile(
    r"\[recipe:Garlic-Infused Olive Oil[^\]]*\]",
    re.I,
)
TAG_RE = re.compile(
    r"(\s+(?:🟢|🟡|🟠|🔴)\s+(?:Fructose|Lactose|Fructans|Galacto-oligosaccharides|Sorbitol|Mannitol)\s+\d+%\s*)$"
)
# Plain oil-only line (qty optional): strip when a recipe link already exists.
# Keeps purposeful variants ("for the tortilla strips", "Extra … and pepitas", etc.).
PLAIN_OIL_ONLY = re.compile(
    r"""^\s*
        (?:Optional:\s*)?
        (?:
            (?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:\.\d+)?|[¼½¾⅓⅔⅛])
            (?:\s*(?:to|-)\s*(?:\d+\s*/\s*\d+|\d+(?:\.\d+)?|[¼½¾⅓⅔⅛]))?
            \s*
        )?
        (?:tbsp|tsp|tablespoons?|teaspoons?|ml|milliliters?|millilitres?|cups?|g|grams?)?
        \s*\.?\s*
        garlic[\s-]*infused[\s-]*(?:olive\s+)?oil
        (?:\s*,?\s*divided)?
        (?:\s*\(optional\))?
        \s*$
    """,
    re.I | re.X,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def is_plain_garlic_oil_dupe(line: str) -> bool:
    """True if line is a plain oil-only duplicate of the recipe-linked oil."""
    if LINK_RE.search(line):
        return False
    body = TAG_RE.sub("", line).rstrip()
    return bool(PLAIN_OIL_ONLY.match(body))


def fix_ingredients(text: str) -> tuple[str | None, list[dict]]:
    if not text:
        return None, []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    has_link = any(LINK_RE.search(line) for line in lines)
    if not has_link:
        return None, []

    changes: list[dict] = []
    out: list[str] = []
    for line in lines:
        if is_plain_garlic_oil_dupe(line):
            changes.append({"was": line, "now": "(removed duplicate; kept recipe link)"})
            continue
        out.append(line)

    if not changes:
        return None, []

    new_text = "\n".join(out)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == text:
        return None, []
    return new_text, changes


def list_recipes() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
          AND ingredients LIKE '%[recipe:Garlic-Infused Olive Oil%'
          AND (
            ingredients LIKE '%garlic-infused%'
            OR ingredients LIKE '%garlic infused%'
            OR ingredients LIKE '%Garlic-Infused%'
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


def update_local(uid: str, ingredients: str) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "UPDATE recipes SET ingredients=?, status=? WHERE uid=?",
        (ingredients, "modified", uid),
    )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recipes = list_recipes()
    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"Fix garlic oil dupes | {mode} | candidates {len(recipes)}")

    results = []
    changed = 0
    updated = 0
    failed = 0
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
            new_ings, changes = fix_ingredients(local.get("ingredients") or "")
            if not new_ings:
                continue
            changed += 1
            results.append(
                {
                    "uid": local["uid"],
                    "name": local["name"],
                    "n_ing": len(changes),
                    "changes": changes[:8],
                }
            )
            if len(samples) < 20:
                for ch in changes[:2]:
                    samples.append(
                        f"{(local['name'] or '')[:36]}: {ch['was'][:55]} → {ch['now'][:40]}"
                    )

            if apply:
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
                cloud_ings, _ = fix_ingredients(rec.get("ingredients") or "")
                if not cloud_ings:
                    continue
                rec["ingredients"] = cloud_ings
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(session, headers, rec, limiter)
                if ok:
                    updated += 1
                    update_local(local["uid"], cloud_ings)
                else:
                    failed += 1
                    safe_print(f"FAIL save {local['name']}")

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

    for s in samples:
        safe_print(f"  {s}")

    summary = {
        "mode": mode,
        "candidates": len(recipes),
        "changed": changed,
        "updated": updated if apply else 0,
        "failed": failed,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recipes": results,
    }
    (OUT_DIR / "_index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    safe_print(
        f"Done | candidates={len(recipes)} changed={changed} "
        f"updated={updated if apply else 0} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
