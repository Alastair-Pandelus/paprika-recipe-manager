"""Rewrite qualifier-first ingredient lines to amount-first, via the sync API.

Paprika reads the start of an ingredient line as the amount (see
docs/recipe-ingredient-format.md). Lines like "Optional: 1 tsp sambal" become
"1 tsp sambal (optional)"; "To serve: 2 tbsp yoghurt" becomes
"2 tbsp yoghurt (to serve)". Only lines whose qualifier is followed by an
amount are touched; every other line, field and recipe is left alone.

Candidates come from the local SQLite (read-only); each is re-read from the
cloud and rewritten from the cloud text. Refuses while the local SQLite has
pending recipes. Writes the before/after list to --report. Dry-run unless
--apply.

  python scripts/tools/fix_qualifier_first_lines.py --report out.tsv
  python scripts/tools/fix_qualifier_first_lines.py --report out.tsv --apply
"""
from __future__ import annotations

import argparse
import asyncio
import csv
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

AMOUNT = r"(?:\d|[½⅓⅔¼¾⅛⅜⅝⅞])"
LINE_RE = re.compile(
    rf"^(?P<lead>\s*)(?P<q>optional|to serve|for serving|garnish)\s*:\s*(?P<rest>{AMOUNT}.*)$",
    re.I,
)
# Inline tags and comments stay at the end, after the new qualifier.
TAIL_RE = re.compile(r"\s*(?://|[🟢🟡🟠🔴♥💚❣💔]).*$")


def rewrite(line: str) -> str | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    rest = m.group("rest").rstrip()
    tail = ""
    t = TAIL_RE.search(rest)
    if t:
        tail = rest[t.start():]
        rest = rest[: t.start()].rstrip()
    qualifier = m.group("q").lower()
    return f"{m.group('lead')}{rest} ({qualifier}){tail}"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def local_candidates(rewrite=rewrite) -> tuple[list[tuple[str, str]], int]:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        pending = con.execute(
            "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
        ).fetchone()[0]
        rows = con.execute(
            "select uid, name, ingredients from recipes where coalesce(in_trash,0)=0 order by name"
        ).fetchall()
    finally:
        con.close()
    out = [(uid, name) for uid, name, ings in rows
           if any(rewrite(l) for l in (ings or "").split("\n"))]
    return out, pending


async def run(rewrite, report: str, apply: bool) -> None:
    """Apply a per-line ``rewrite`` (returns None to leave a line) to every
    recipe with a matching line, through the sync API."""
    args = argparse.Namespace(report=report, apply=apply)
    candidates, pending = local_candidates(rewrite)
    safe_print(f"Candidate recipes: {len(candidates)}; local pending recipes: {pending}")
    if pending and args.apply:
        raise SystemExit("Local SQLite has pending recipes; not applied.")

    user, pw = paprika_credentials()
    lim = RateLimiter(0.6)
    changes: list[tuple[str, str, str, str]] = []
    updated = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for uid, _ in candidates:
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            recipe = (body or {}).get("result")
            if st != 200 or not recipe or recipe.get("in_trash"):
                continue
            lines = (recipe.get("ingredients") or "").split("\n")
            new_lines = []
            for l in lines:
                n = rewrite(l)
                if n is not None and n != l:
                    changes.append((recipe["name"], uid, l, n))
                new_lines.append(n if n is not None else l)
            if new_lines == lines or not args.apply:
                continue
            new = dict(recipe, ingredients="\n".join(new_lines))
            new["hash"] = calc_hash(new)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip.compress(json.dumps(new, separators=(",", ":")).encode()),
                content_type="application/octet-stream",
                filename="data",
            )
            await lim.wait_turn()
            async with session.post(url, headers=headers, data=form) as r:
                txt = await r.text()
                if '"result":true' not in txt.replace(" ", ""):
                    raise SystemExit(f"POST failed for {recipe['name']}: {r.status} {txt[:300]}")
            updated += 1

        if args.apply and updated:
            await lim.wait_turn()
            async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
                safe_print("notify", r.status)

    with open(args.report, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["recipe", "uid", "before", "after"])
        w.writerows(changes)
    safe_print(
        f"Lines: {len(changes)} in {len({c[1] for c in changes})} recipes"
        + (f"; recipes updated: {updated}" if args.apply else " (dry-run)")
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    await run(rewrite, args.report, args.apply)


if __name__ == "__main__":
    asyncio.run(main())
