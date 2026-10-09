"""Fix Prep-bowl cook steps to say '{label} bowl' (spice mix bowl, etc.).

  python scripts/tools/fix_prep_bowl_refs.py
  python scripts/tools/fix_prep_bowl_refs.py --apply
  python scripts/tools/fix_prep_bowl_refs.py --apply --name "Carrot Hummus"
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

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

# In cook steps only: bare label → label + ' bowl'
COOK_FIXES = [
    (re.compile(r"\bspice mix(?!\s+bowl)\b", re.I), "spice mix bowl"),
    (re.compile(r"\baromatics(?!\s+bowl)\b", re.I), "aromatics bowl"),
    (re.compile(r"\bmash seasoning(?!\s+bowl)\b", re.I), "mash seasoning bowl"),
    (re.compile(r"\bsauce herbs(?!\s+bowl)\b", re.I), "sauce herbs bowl"),
    (re.compile(r"\bflour mix(?!\s+bowl)\b", re.I), "flour mix bowl"),
]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def fix_directions(directions: str) -> tuple[str, list[str]]:
    """Return (new_directions, list of fix descriptions)."""
    d = directions or ""
    m = re.search(r"\*\*1\.", d)
    if not m:
        return d, []
    head, cook = d[: m.start()], d[m.start() :]
    notes: list[str] = []
    new_cook = cook
    for rx, repl in COOK_FIXES:
        new_cook, n = rx.subn(repl, new_cook)
        if n:
            notes.append(f"{repl}×{n}")
    if new_cook == cook:
        return d, []
    return head + new_cook, notes


def hummus_extra_fix(directions: str) -> str:
    """Carrot hummus: chickpeas / mint / spice mix bowl wording."""
    d = directions or ""
    d = re.sub(
        r"with the oil, salt and spice mix\b",
        "with the oil, salt and spice mix bowl",
        d,
        flags=re.I,
    )
    d = re.sub(
        r"with the chickpeas,",
        "with the chickpeas bowl,",
        d,
        flags=re.I,
    )
    d = re.sub(
        r"Garnish with mint and",
        "Garnish with mint from the finish bowl and",
        d,
        flags=re.I,
    )
    return d


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--name", type=str, default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if args.name:
        rows = con.execute(
            """
            SELECT uid, name, directions, ingredients, description, notes
            FROM recipes
            WHERE coalesce(in_trash,0)=0
              AND directions LIKE '%Prep (bowls)%'
              AND name LIKE ?
            ORDER BY name COLLATE NOCASE
            """,
            (f"%{args.name}%",),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT uid, name, directions, ingredients, description, notes
            FROM recipes
            WHERE coalesce(in_trash,0)=0
              AND directions LIKE '%Prep (bowls)%'
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()
    con.close()
    if args.limit > 0:
        rows = rows[: args.limit]

    changes: list[tuple[sqlite3.Row, str, list[str]]] = []
    for r in rows:
        d = r["directions"] or ""
        if "Carrot Hummus" in (r["name"] or ""):
            d2 = hummus_extra_fix(d)
            d2, notes = fix_directions(d2)
            if d2 != (r["directions"] or ""):
                if not notes:
                    notes = ["hummus bowl wording"]
                changes.append((r, d2, notes))
            continue
        d2, notes = fix_directions(d)
        if notes:
            changes.append((r, d2, notes))

    mode = "APPLY" if args.apply else "DRY-RUN"
    safe_print(f"Prep bowl refs | {mode} | candidates={len(changes)} / scanned={len(rows)}")
    for r, _d2, notes in changes[:40]:
        safe_print(f"  {r['name'][:70]}  [{', '.join(notes)}]")
    if len(changes) > 40:
        safe_print(f"  … +{len(changes) - 40} more")

    if not args.apply or not changes:
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.28)
    updated = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
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

        local = sqlite3.connect(DB)
        for i, (r, new_dirs, _notes) in enumerate(changes, 1):
            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{r['uid']}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                safe_print(f"FAIL missing {r['name']}")
                continue
            # Re-apply transform on cloud text
            cloud_dirs = rec.get("directions") or ""
            if "Carrot Hummus" in (rec.get("name") or r["name"] or ""):
                fixed = hummus_extra_fix(cloud_dirs)
                fixed, _ = fix_directions(fixed)
            else:
                fixed, notes = fix_directions(cloud_dirs)
                if not notes:
                    fixed = new_dirs
            rec["directions"] = fixed
            rec["hash"] = calc_hash(rec)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{rec['uid']}/",
                headers=headers,
                data=form,
            ) as resp:
                ok = '"result":true' in (await resp.text()).replace(" ", "")
            if ok:
                updated += 1
                local.execute(
                    "UPDATE recipes SET directions=?, status=? WHERE uid=?",
                    (fixed, "modified", r["uid"]),
                )
                local.commit()
            else:
                safe_print(f"FAIL save {r['name']}")
            if i % 40 == 0 or i == len(changes):
                safe_print(f"progress {i}/{len(changes)} updated={updated}")

        local.close()
        await limiter.wait_turn()
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
        ) as resp:
            await resp.text()
    safe_print(f"Done | updated={updated}")


if __name__ == "__main__":
    asyncio.run(main())
