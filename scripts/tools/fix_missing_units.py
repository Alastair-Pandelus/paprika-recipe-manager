"""Restore units lost by the old 1-serving rescale, from each recipe's source website.

A line like "1/4 olive oil", "3/4 chicken stock" or "1/6 ½ cups basmati rice" lost its
unit (or kept a stray rescale fraction). Only those lines change: each is matched to its
source line, the source amount and unit are scaled to the recipe's Paprika servings, and
the line keeps its own wording and tags. Counts ("20 blueberries"), stock cubes and
ranges are left alone; lines with no confident source match are listed, not changed.

  python scripts/tools/fix_missing_units.py --report docs/missing-units.tsv [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from cleanup_library import (LOCAL_DB, TAG_RE, calc_hash, fetch_source, format_line, scale_line,  # noqa: E402
                             split_amount, tokens)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from rescore_fodmap import tidy_amount  # noqa: E402

UNIT = (r"(?:g|gr|grams?|kg|ml|l|litres?|liters?|tsp|tbsp|teaspoons?|tablespoons?|cups?|oz|ounces?|lbs?|pounds?|"
        r"pinch(?:es)?|dash(?:es)?|cans?|tins?|jars?|packs?|packets?|packages?|bags?|sachets?|cubes?|pots?|bunch(?:es)?|"
        r"slices?|sheets?|scoops?|sprigs?|stalks?|handfuls?|cloves?|fillets?|pieces?|heads?|knobs?|bottles?|servings?|portions?)")
NUM = r"(?:\d+\s*[½⅓⅔¼¾⅛⅜⅝⅞]|\d+(?:[.,]\d+)?(?:\s+\d+/\d+|/\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
LEAD = re.compile(rf"^\s*({NUM})\s+(.*)$")
DOUBLE = re.compile(rf"^\s*\d+/\d+\s+({NUM}\s*{UNIT}\b.*)$", re.I)
RANGE = re.compile(rf"^\s*{NUM}\s*(?:to|or|-|–|&#8211;)\s*{NUM}", re.I)


def needs_unit(body: str) -> bool:
    if RANGE.match(body):
        return False
    if DOUBLE.match(body):
        return True
    m = LEAD.match(body)
    if not m:
        return False
    rest = m.group(2)
    if re.match(r"(medium|large|small|whole|extra|big|heaped|level)\b", rest, re.I):
        return False  # a count with a size word
    return not re.match(rf"{UNIT}\b|x\s*\d|\d|[½⅓⅔¼¾⅛⅜⅝⅞]", rest, re.I)


def _value(amount: str) -> float | None:
    from fractions import Fraction
    a = amount.strip()
    for k, v in {"½": " 1/2", "⅓": " 1/3", "⅔": " 2/3", "¼": " 1/4", "¾": " 3/4", "⅛": " 1/8"}.items():
        a = a.replace(k, v)
    try:
        return float(sum(Fraction(p) for p in a.split()))
    except (ValueError, ZeroDivisionError):
        return None


def src_unit(src_body: str) -> bool:
    amt, rest = split_amount(src_body)
    return bool(amt) and bool(re.search(rf"\b{UNIT}\b", amt + " " + rest.split(" ", 1)[0], re.I))


def fix_line(old: str, src_lines: list[str], factor: float, used: set[int]) -> tuple[str | None, str]:
    body = TAG_RE.sub("", old).strip()
    tag = old[len(TAG_RE.sub("", old)):]
    dbl = DOUBLE.match(body)
    rest_old = body
    m = LEAD.match(body)
    if m:
        rest_old = m.group(2)
    if dbl:
        rest_old = split_amount(dbl.group(1))[1]
    ot = tokens(rest_old)
    best, score = None, 0.0
    for i, s in enumerate(src_lines):
        if i in used:
            continue
        st = tokens(split_amount(s)[1])
        if not st or not ot:
            continue
        inter = len(ot & st)
        if inter / min(len(ot), len(st)) >= 0.6 and inter / len(ot | st) > score:
            best, score = i, inter / len(ot | st)
    if best is None:
        return None, "no source match"
    s_body = format_line(src_lines[best])
    if not src_unit(s_body):
        return None, f"source has no unit either: {src_lines[best]}"
    scaled = scale_line(s_body, factor) if abs(factor - 1) > 1e-9 else s_body
    if not scaled:
        return None, "could not scale"
    amt, s_rest = split_amount(tidy_amount(scaled))
    u = re.match(rf"^\s*({UNIT})\b\s*(.*)$", s_rest, re.I)
    unit_in_amt = re.search(rf"\b({UNIT})\b", amt, re.I)
    unit_word = (unit_in_amt.group(1) if unit_in_amt else (u.group(1) if u else ""))
    amt_num = re.sub(rf"\s*\b{UNIT}\b.*$", "", amt, flags=re.I).strip()
    rest_old_clean = re.sub(rf"^\s*{UNIT}\b\s*", "", rest_old, flags=re.I)
    if not dbl:
        # Agreement check: the line's own number with the source unit should be close to
        # the scaled source amount; if not, the old rescale made it unreliable -> review.
        old_v = _value(m.group(1)) if m else None
        new_v = _value(amt_num)
        if not old_v or not new_v or not (0.67 <= old_v / new_v <= 1.5):
            return None, f"review: line says {m.group(1) if m else '?'}, source scaled gives {amt_num} {unit_word} ({src_lines[best]})"
        amt_num = m.group(1)  # keep the line's own number; add the unit only
    if unit_word and re.search(rf"\b{re.escape(unit_word.rstrip('s'))}s?\b", rest_old_clean, re.I):
        return None, "unit already in the text"
    used.add(best)
    new = f"{amt_num} {unit_word} {rest_old_clean}".replace("  ", " ").strip()
    if new == body:
        return None, "no change"
    return new + tag, src_lines[best]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    todo = []
    for uid, name, ing, src, serv in con.execute(
            "select uid, name, ingredients, coalesce(source_url,''), servings from recipes where coalesce(in_trash,0)=0"):
        lines = [l for l in (ing or "").split("\n") if needs_unit(TAG_RE.sub("", l).strip())]
        if lines:
            todo.append((uid, name, src, serv, lines))
    print(f"{len(todo)} recipes, {sum(len(t[4]) for t in todo)} lines")
    report, last, fixes = [], {}, {}
    for uid, name, src, serv, lines in todo:
        if not src.startswith("http"):
            report += [[name, l, "", "no source website"] for l in lines]; continue
        host = urlparse(src).netloc
        time.sleep(max(0, 1.0 - (time.time() - last.get(host, 0))))
        last[host] = time.time()
        try:
            src_lines, y = fetch_source(src)
        except Exception as e:  # noqa: BLE001
            report += [[name, l, "", f"fetch failed: {str(e)[:50]}"] for l in lines]; continue
        if not src_lines:
            report += [[name, l, "", "source has no ingredient list"] for l in lines]; continue
        factor = (f.parse_servings(serv) or 1) / (y or f.parse_servings(serv) or 1)
        used: set[int] = set()
        for l in lines:
            new, why = fix_line(l, src_lines, factor, used)
            report.append([name, TAG_RE.sub("", l).strip(), TAG_RE.sub("", new).strip() if new else "", why])
            if new:
                fixes.setdefault(uid, {})[l] = new
    with open(a.report, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t"); w.writerow(["recipe", "before", "after", "source line / reason"]); w.writerows(report)
    print(f"fixable lines: {sum(len(v) for v in fixes.values())} in {len(fixes)} recipes; report -> {a.report}")
    if not a.apply or not fixes:
        return
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        for uid, reps in fixes.items():
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, b = await api_json(s, lim, "GET", url, headers=h); r = b["result"]
            lines = r["ingredients"].split("\n")
            new = [reps.get(l, l) for l in lines]
            if new == lines:
                continue
            t = f.transform_recipe(r["name"], "\n".join(new), r.get("description") or "", r.get("notes") or "", servings=r.get("servings"))
            t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], r.get("notes") or "", servings=r.get("servings"))
            with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "ingredients")}, ensure_ascii=False) + "\n")
            upd = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"]); upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData(); form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()), content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), r["name"]
            if t2["name"] != r["name"]:
                print("marker", r["name"], "->", t2["name"])
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
            print("notify", resp.status)


if __name__ == "__main__":
    asyncio.run(main())
