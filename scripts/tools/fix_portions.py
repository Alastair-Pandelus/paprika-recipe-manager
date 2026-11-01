"""Fix portion errors in ❌ low FODMAP recipes against their website.

For each recipe in docs/lowfodmap-conflicts.tsv: read the website's
ingredients and servings (JSON-LD yield, else the page's "1/N of the
recipe" / "serves N" note). Where Paprika's per-serving amount of a driving
ingredient differs from the website's (wrong servings, unscaled or
mis-scaled line), the recipe goes back to the website's servings and
amounts via cleanup_library.rebuild, keeping its own wording, tags and
noted deliberate changes. FODMAP and sat-fat tags are then rescored.

Through the sync API only; backs up first. Dry-run unless --apply.

  python scripts/tools/fix_portions.py --report r.tsv --backup b.jsonl [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import cholesterol_score_lib as chol  # noqa: E402
import fodmap_score_lib as fodmap  # noqa: E402
from cleanup_library import (  # noqa: E402
    LOCAL_DB, TAG_RE, amount_value, calc_hash, fetch_source, format_line, rebuild,
    split_amount, tokens,
)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from source_groups import fetch_page  # noqa: E402

PAGE_YIELD = [
    re.compile(r"\b1\s*/\s*(\d+)(?:th)?\s+of\s+the\s+recipe", re.I),
    re.compile(r"\bserves\s+(\d+)\b", re.I),
    re.compile(r"\bfor\s+(\d+)\s+(?:people|persons|portions|servings)\b", re.I),
    re.compile(r"\b(\d+)\s+(?:servings|portions)\b", re.I),
]


def page_yield(url: str) -> int | None:
    page = fetch_page(url)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ",
                  re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S))))
    for pat in PAGE_YIELD:
        m = pat.search(text)
        if m and 1 < int(m.group(1)) <= 24:
            return int(m.group(1))
    return None


def per_serving(line: str, servings: float) -> tuple[str, float] | None:
    a = split_amount(TAG_RE.sub("", line))[0]
    v = amount_value(a) if a else None
    return (v[0], v[1] / servings) if v else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conflicts", default=str(ROOT / "docs" / "lowfodmap-conflicts.tsv"))
    ap.add_argument("--report", required=True)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    pending = con.execute(
        "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
    ).fetchone()[0]
    by_name = {n: (u, s) for u, n, s in con.execute(
        "select uid, name, source_url from recipes where coalesce(in_trash,0)=0")}
    con.close()
    if pending and args.apply:
        raise SystemExit("Local SQLite has pending recipes; not applied.")
    with open(args.conflicts, encoding="utf-8") as f:
        names = [r[0] for r in list(csv.reader(f, delimiter="\t"))[1:]]

    report, written = [], 0
    backup = open(args.backup, "a", encoding="utf-8") if args.apply else None
    user, pw = paprika_credentials()
    lim = RateLimiter(0.6)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for name in names:
            if name not in by_name:
                report.append([name, "skip", "renamed since the list", "", ""])
                continue
            uid, src = by_name[name]
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            r = (body or {}).get("result")
            if not r:
                continue
            if not r["name"].startswith("❌"):
                report.append([r["name"], "already fixed", "", "", ""])
                continue
            try:
                src_lines, y = fetch_source(src) if src else ([], None)
                y = y or (page_yield(src) if src else None)
            except Exception as e:  # noqa: BLE001
                report.append([r["name"], "website unreachable", str(e)[:60], "", ""])
                continue
            if not src_lines or not y:
                report.append([r["name"], "website servings unknown", src or "", "", ""])
                continue
            serv = fodmap.parse_servings(r.get("servings"))
            lines = r["ingredients"].split("\n")
            notes = r.get("notes") or ""
            # Driving lines: amber/red FODMAP tags.
            drivers = [l for l in lines if re.search("[🟠🔴]", l)]
            mismatch = []
            for d in drivers:
                dt = tokens(split_amount(TAG_RE.sub("", d))[1])
                # deliberate, noted change: leave it
                if any(t in notes.lower() for t in dt) and re.search(r"Low FODMAP:|Changed:", notes):
                    continue
                best = max(src_lines, key=lambda s: len(dt & tokens(split_amount(format_line(s))[1])), default=None)
                if not best or not dt & tokens(split_amount(format_line(best))[1]):
                    continue
                mine, theirs = per_serving(d, serv), per_serving(format_line(best), y)
                if mine and theirs and mine[0] == theirs[0] and theirs[1]:
                    ratio = mine[1] / theirs[1]
                    if not 0.75 <= ratio <= 1.33:
                        mismatch.append((d, best, ratio))
            if not mismatch:
                report.append([r["name"], "same portion as website", f"servings {r.get('servings')} vs site {y}",
                               " | ".join(drivers), ""])
                continue
            factor = y / serv
            new_lines, notes_ = rebuild(lines, src_lines, factor, {d for d, _, _ in mismatch}, set())
            servings = str(y)
            ing = "\n".join(new_lines)
            t = fodmap.transform_recipe(r["name"], ing, r.get("description") or "", notes, servings=servings)
            t2 = chol.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=servings)
            new = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"],
                       servings=servings)
            report.append([r["name"], f"portion fixed -> {new['name'].split(' ')[0]}",
                           f"servings {r.get('servings')} -> {servings}; " +
                           "; ".join(f"{d.split('  ')[0]} was x{ratio:.2f} of site ({b})" for d, b, ratio in mismatch),
                           " | ".join(l for l in new["ingredients"].split("\n") if re.search("[🟠🔴]", l)),
                           " / ".join(notes_)])
            if not args.apply:
                continue
            backup.write(json.dumps({k: r.get(k) for k in ("uid", "name", "servings", "ingredients", "description")},
                                    ensure_ascii=False) + "\n")
            backup.flush()
            new["hash"] = calc_hash(new)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(new, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with session.post(url, headers=headers, data=form) as resp:
                if '"result":true' not in (await resp.text()).replace(" ", ""):
                    raise SystemExit(f"POST failed for {r['name']}")
            written += 1
        if args.apply and written:
            await lim.wait_turn()
            async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as resp:
                safe_print("notify", resp.status)
    if backup:
        backup.close()
    with open(args.report, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["recipe", "result", "detail", "high lines now", "rebuild notes"])
        w.writerows(report)
    kinds: dict[str, int] = {}
    for x in report:
        kinds[x[1]] = kinds.get(x[1], 0) + 1
    safe_print(f"Report: {kinds}" + (f"; written {written}" if args.apply else " (dry-run)"))


if __name__ == "__main__":
    asyncio.run(main())
