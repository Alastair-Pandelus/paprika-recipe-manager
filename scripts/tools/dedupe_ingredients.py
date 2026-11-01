"""Remove duplicate ingredient lines (docs/recipe-ingredient-format.md, rule 3).

Within a section (lines between headers ending in ":"), two lines naming the
same ingredient are a copy when one has no amount, or the amounts match once
units are converted (metric and imperial copies, "tsp" vs "teaspoon"). The
first copy stays, taking over a FODMAP or sat-fat tag from the removed one if
it has none. Same ingredient with clearly different amounts, or in another
section (cake and icing, oil for frying and dressing), is left alone and
listed as a genuine repeat.

Through the sync API only; refuses while the local SQLite has pending
recipes; backs each recipe up to --backup before writing. Dry-run unless
--apply.

  python scripts/tools/dedupe_ingredients.py --report r.tsv --backup b.jsonl [--apply]
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

from cleanup_library import (  # noqa: E402
    LOCAL_DB, TAG_RE, amount_value, calc_hash, split_amount, tokens,
)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "half", "inch", "max", "per",
                "serving", "use", "make", "sure", "stay", "below", "total"}


DESCRIPTORS = {"virgin", "plain", "pure", "raw", "frozen",
               "canned", "chopped", "diced", "sliced", "minced", "grated", "medium",
               "large", "small", "whole", "finely", "roughly", "thinly", "coarse",
               "fine", "light", "unsalted", "salted", "organic", "additional"}


def ingredient_key(line: str) -> frozenset[str]:
    """The ingredient's core name: text before the first comma, without
    descriptors, so "1/8 cup fresh cilantro, chopped" and "1/8 cup chopped
    coriander" agree but "green bell pepper" and "red bell pepper" don't."""
    body = TAG_RE.sub("", html.unescape(line))
    if re.search(r"\breplaces\b", body, re.I):
        return frozenset()  # the library's own substitution line
    _, rest = split_amount(body)
    core = re.split(r",|\s[-–]\s|\(", rest, maxsplit=1)[0]
    key = {t for t in tokens(core) if t not in NUMBER_WORDS and t not in DESCRIPTORS}
    if re.search(r"\bbell\b", core, re.I):
        key.add("bell")  # tokens() folds "bell pepper" into "pepper"
    return frozenset(key)


PURPOSE = re.compile(r"\b(?:extra|serving|serve|finish|garnish|frying|brushing|dusting|"
                     r"divided|decorat\w*|topping|for the)\b", re.I)


def different_use(a_line: str, b_line: str) -> bool:
    """Same ingredient for another job: "Extra soy sauce", "for frying",
    "butter, softened" next to "butter, melted"."""
    a, b = (TAG_RE.sub("", html.unescape(x)) for x in (a_line, b_line))
    open_a, open_b = (re.sub(r"[()]", " ", x) for x in (a, b))
    if (PURPOSE.search(a) or PURPOSE.search(b)) and tokens(open_a) != tokens(open_b):
        return True
    tail = [tokens(re.sub(r"\([^)]*\)", " ", x.split(",", 1)[1])) if "," in x else set()
            for x in (a, b)]
    return bool(tail[0] and tail[1] and tail[0] != tail[1])


def same_ingredient(a: frozenset[str], b: frozenset[str]) -> bool:
    return bool(a) and a == b


def amounts_match(a_line: str, b_line: str) -> bool | None:
    """True: same amount; False: clearly different; None: one has none."""
    a_amt, _ = split_amount(TAG_RE.sub("", a_line))
    b_amt, _ = split_amount(TAG_RE.sub("", b_line))
    if not a_amt or not b_amt:
        return None
    av, bv = amount_value(a_amt), amount_value(b_amt)
    if av is None or bv is None:
        return a_amt.lower() == b_amt.lower()
    if av[0] == bv[0]:
        return bv[1] > 0 and 0.9 <= av[1] / bv[1] <= 1.11
    if {av[0], bv[0]} == {"vol", "wt"}:
        # A metric/imperial pair: grams per ml within a plausible density.
        wt, vol = (av, bv) if av[0] == "wt" else (bv, av)
        return vol[1] > 0 and 0.4 <= wt[1] / vol[1] <= 1.5
    return False


def dedupe(lines: list[str]) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]]]:
    """Returns (new lines, removed (kept, removed) pairs, repeats to list)."""
    out: list[str] = []
    kept: list[tuple[int, int, frozenset[str]]] = []  # (out index, section, key)
    removed, repeats = [], []
    section = 0
    for line in lines:
        if line.strip().endswith(":"):
            section += 1
        key = ingredient_key(line) if line.strip() and not line.strip().endswith(":") else frozenset()
        match = next(((i, s) for i, s, k in kept if same_ingredient(k, key)), None)
        if match is None:
            if key:
                kept.append((len(out), section, key))
            out.append(line)
            continue
        i, s = match
        same_amount = amounts_match(out[i], line)
        if s != section or same_amount is False or different_use(out[i], line):
            repeats.append((out[i], line))
            out.append(line)
            continue
        # A copy. Keep the first, with an amount and a tag if either line has one.
        first = out[i]
        if not split_amount(TAG_RE.sub("", first))[0] and split_amount(TAG_RE.sub("", line))[0]:
            first, line_ = line, first
        else:
            line_ = line
        f_body = TAG_RE.sub("", first)
        l_tag = line_[len(TAG_RE.sub("", line_)):]
        if f_body == first and l_tag.strip():
            first = f"{first.rstrip()}  {l_tag.strip()}"
        removed.append((first, line_))
        out[i] = first
    return out, removed, repeats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    pending = con.execute(
        "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
    ).fetchone()[0]
    rows = con.execute("select uid, ingredients from recipes where coalesce(in_trash,0)=0").fetchall()
    con.close()
    safe_print(f"Local pending recipes: {pending}")
    if pending and args.apply:
        raise SystemExit("not applied")
    candidates = [uid for uid, ings in rows
                  if (lambda r: r[1] or r[2])(dedupe((ings or "").split("\n")))]
    safe_print(f"Recipes with repeated ingredients (local copy): {len(candidates)}")

    report, written = [], 0
    backup = open(args.backup, "a", encoding="utf-8") if args.apply else None
    user, pw = paprika_credentials()
    lim = RateLimiter(0.6)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for uid in candidates:
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            recipe = (body or {}).get("result")
            if st != 200 or not recipe:
                continue
            old = (recipe.get("ingredients") or "").split("\n")
            new, removed, repeats = dedupe(old)
            for k, r in removed:
                report.append([uid, recipe["name"], "removed copy", r, f"kept: {k}"])
            for a, b in repeats:
                report.append([uid, recipe["name"], "genuine repeat, kept both", a, b])
            if new == old or not args.apply:
                continue
            backup.write(json.dumps({"uid": uid, "name": recipe["name"], "servings": recipe.get("servings"),
                                     "ingredients": recipe.get("ingredients")}, ensure_ascii=False) + "\n")
            backup.flush()
            upd = dict(recipe, ingredients="\n".join(new))
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with session.post(url, headers=headers, data=form) as r:
                txt = await r.text()
                if '"result":true' not in txt.replace(" ", ""):
                    raise SystemExit(f"POST failed for {recipe['name']}: {r.status} {txt[:300]}")
            written += 1
        if args.apply and written:
            await lim.wait_turn()
            async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
                safe_print("notify", r.status)
    if backup:
        backup.close()
    with open(args.report, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["uid", "recipe", "kind", "line", "other"])
        w.writerows(report)
    kinds: dict[str, int] = {}
    for r in report:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    safe_print(f"Report: {kinds}" + (f"; recipes written: {written}" if args.apply else " (dry-run)"))


if __name__ == "__main__":
    asyncio.run(main())
