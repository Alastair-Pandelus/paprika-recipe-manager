"""Bold numbered cook-step titles in Prep-bowl Main Meals directions.

Transforms: 1. Brown: Heat...  →  1. **Brown:** Heat...
Leaves steps without a short Title: colon unchanged.
"""
from __future__ import annotations

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
MAIN_MEALS = "9754B607-D3B4-46BB-9289-ADE50C4F098E"

# 1. **Title:** body  OR  1. Title: body  →  **1. Title:** body
STEP_RE = re.compile(
    r"(?m)^(?P<n>\d+)\.\s+(?:\*\*)?(?P<title>[^*:\n]{1,50}?)(?::\*\*|\*\*:|:)\s+(?P<body>\S.*)$"
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def bold_step_titles(directions: str) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        title = m.group("title").strip()
        if title.count(" ") > 5:
            return m.group(0)
        count += 1
        return f"**{m.group('n')}. {title}:** {m.group('body')}"

    new = STEP_RE.sub(repl, directions or "")
    # Normalize already-partial form 1. **Title:** → **1. Title:**
    new, n2 = re.subn(
        r"(?m)^(?P<n>\d+)\.\s+\*\*(?P<title>[^*:\n]+):\*\*\s*(?P<body>.*)$",
        lambda m: f"**{m.group('n')}. {m.group('title').strip()}:** {m.group('body')}",
        new,
    )
    count += n2
    new2, n = re.subn(
        r"(?m)^Prep \(bowls\):",
        "**Prep (bowls):**",
        new,
        count=1,
    )
    count += n

    def bowl_repl(m: re.Match[str]) -> str:
        nonlocal count
        if m.group(0).startswith("**"):
            return m.group(0)
        count += 1
        return f"**{m.group(1)}:**"

    new3 = re.sub(
        r"(?m)^((?:Large|Medium|Small) bowl\s*[—–-]\s*[^:\n]+):",
        bowl_repl,
        new2,
    )
    return new3, count


async def post_recipe(session, headers, recipe: dict) -> bool:
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
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


async def main() -> None:
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT r.uid, r.name, r.directions
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        WHERE rtc.category_uid = ? AND r.in_trash = 0
          AND r.directions LIKE 'Prep (bowls)%'
        ORDER BY r.name COLLATE NOCASE
        """,
        (MAIN_MEALS,),
    ).fetchall()

    updates = []
    total_hits = 0
    for r in rows:
        new_d, n = bold_step_titles(r["directions"] or "")
        if n and new_d != (r["directions"] or ""):
            updates.append((r["uid"], r["name"], new_d, n))
            total_hits += n

    safe_print(f"candidates {len(rows)}; recipes to update {len(updates)}; bold marks {total_hits}")
    for uid, name, new_d, n in updates[:2]:
        safe_print(f"\nSAMPLE {name} ({n} marks)")
        for ln in new_d.splitlines():
            if ln.startswith("**Prep") or ln.startswith("**Large") or re.match(r"^\d+\. \*\*", ln):
                safe_print(" ", ln[:100])

    if dry:
        safe_print("dry-run only")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.25)
    ok_n = fail_n = 0
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}
        for i, (uid, name, new_d, _n) in enumerate(updates, 1):
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = body.get("result") or {}
            if not rec.get("uid"):
                fail_n += 1
                continue
            rec["directions"] = new_d
            rec["hash"] = calc_hash(rec)
            await limiter.wait_turn()
            ok = await post_recipe(s, H, rec)
            if ok:
                ok_n += 1
                con.execute(
                    "UPDATE recipes SET directions=?, status=? WHERE uid=?",
                    (new_d, "unmodified", uid),
                )
            else:
                fail_n += 1
            if i % 50 == 0 or i == len(updates):
                safe_print(f"progress {i}/{len(updates)} ok={ok_n} fail={fail_n}")
                con.commit()
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
    con.commit()
    con.close()
    safe_print(f"done ok={ok_n} fail={fail_n}")


if __name__ == "__main__":
    asyncio.run(main())
