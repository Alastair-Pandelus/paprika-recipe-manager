"""Rationalise repeated ingredients back to the source website.

Another tool appended copies of ingredient lines at the end of recipes. For
each recipe with a repeated ingredient (same core name, see
dedupe_ingredients.ingredient_key):

- Website lists it once: one line stays, in the first line's position and
  unit, with the website's amount at the recipe's servings; a note in
  brackets from a copy is carried over; FODMAP and sat-fat tags are scaled
  to the new amount. The copies go.
- Website lists it the same number of times: each line takes the website's
  amount in order, and the website's purpose ("for sprinkling") is added
  when the line lacks it.
- No website line (or no website): later copies are dropped and the first
  kept, unless a line is one of the library's substitutions ("replaces",
  spring onion greens or leek for onion), which always stay.
- Anything else is left and listed.

Lines that name their own use ("for sprinkling", "to serve", "for the
glaze") are a separate use and never merged.

Through the sync API only; refuses while the local SQLite has pending
recipes; backs each recipe up before writing. Dry-run unless --apply.

  python scripts/tools/rationalise_repeats.py --report r.tsv --backup b.jsonl [--apply]
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
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from cholesterol_score_lib import chol_emoji_ingredient, format_sat_tag  # noqa: E402
from cleanup_library import (  # noqa: E402
    LOCAL_DB, TAG_RE, UNIT_BASE, amount_value, calc_hash, fetch_source, fmt_num,
    format_line, split_amount,
)
from dedupe_ingredients import ingredient_key  # noqa: E402
from fodmap_score_lib import emoji_for  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

USE = re.compile(
    r"\b(?:for (?:the |)?\w+ing\b|for (?:the )?(?:glaze|icing|frosting|dressing|sauce|"
    r"marinade|topping|crust|filling|garnish|tortilla strips|rice|crumble)|to serve|"
    r"for serving|to finish|to taste|to (?:thin|loosen|brush|dust|sprinkle|drizzle)|"
    r"if needed|extra)\b", re.I)
SUBST = re.compile(r"\breplaces\b|spring onion green|\bleek\b", re.I)
FODMAP_TAG = re.compile(r"([🟢🟡🟠🔴])\s+([A-Za-z-]+)\s+(\d+)%")
SAT_TAG = re.compile(r"[♥💚❣💔]\s+Sat fat\s+([\d.]+)g\s+\(\d+%\)")


def servings_number(s) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", str(s or ""))
    return float(m.group()) if m else None


def use_of(line: str) -> str:
    m = USE.search(TAG_RE.sub("", line))
    return m.group(0).lower() if m else ""


def scale_tags(tag: str, ratio: float) -> str:
    """FODMAP % and sat-fat grams are per serving, so they scale with the amount."""
    def fod(m: re.Match) -> str:
        r = int(m.group(3)) * ratio / 100
        return f"{emoji_for(r)} {m.group(2)} {int(round(r * 100))}%"

    def sat(m: re.Match) -> str:
        g = float(m.group(1)) * ratio
        return format_sat_tag(chol_emoji_ingredient(g), g)

    return SAT_TAG.sub(sat, FODMAP_TAG.sub(fod, tag))


def fmt_qty(v: float, unit: str) -> str:
    """Grams and ml as plain numbers; spoons and cups as fractions; a pinch
    below 1/16 tsp."""
    u = unit.lower()
    fam = UNIT_BASE.get(u)
    if fam and fam[0] == "vol":
        ml = v * fam[1]
        if ml < 0.3:
            return "1 pinch"
        # Small amounts in a smaller spoon: never "0 cup".
        if fam[1] >= 240 and ml < 60:
            u, unit, v = "tbsp", "tbsp", ml / 15
        if u in ("tbsp", "tbsps", "tablespoon", "tablespoons") and ml < 7.5:
            u, unit, v = "tsp", "tsp", ml / 5
    if u in ("g", "gr", "gram", "grams", "ml"):
        s = str(int(round(v))) if v >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s} {u}"
    return f"{fmt_num(v)} {unit}".strip()


def target_amount(src_line: str, factor: float, like_amt: str) -> tuple[str, float] | None:
    """Website amount x factor, in like_amt's unit when the units convert.
    Returns (amount text, base value)."""
    s_amt, _ = split_amount(TAG_RE.sub("", src_line))
    sv = amount_value(s_amt) if s_amt else None
    if sv is None:
        return None
    base = sv[1] * factor
    lv = amount_value(like_amt) if like_amt else None
    m = re.match(r"^.*?\s([a-z]+)\.?$", like_amt or "", re.I)
    unit = m.group(1).lower() if m else ""
    if lv and lv[0] == sv[0] and unit in UNIT_BASE:
        return fmt_qty(base / UNIT_BASE[unit][1], unit), base
    s_unit = re.match(r"^.*?\s([a-z]+)\.?$", s_amt, re.I)
    s_unit = s_unit.group(1) if s_unit and s_unit.group(1).lower() in UNIT_BASE else ""
    raw = sv[2] * factor
    return fmt_qty(raw, s_unit), base


def set_amount(line: str, new_amt: str, new_base: float | None) -> str:
    body = TAG_RE.sub("", line)
    tag = line[len(body):]
    amt, rest = split_amount(body)
    ov = amount_value(amt) if amt else None
    if tag.strip() and ov and new_base and ov[1]:
        tag = "  " + scale_tags(tag.strip(), new_base / ov[1])
    return format_line(f"{new_amt} {rest}") + tag


def notes_of(line: str) -> list[str]:
    return re.findall(r"\(([^()]*)\)", TAG_RE.sub("", line))


def rationalise(lines: list[str], src_lines: list[str], factor: float | None
                ) -> tuple[list[str], list[list[str]]]:
    out = list(lines)
    report: list[list[str]] = []
    groups: dict[frozenset, list[int]] = {}
    for i, l in enumerate(lines):
        if l.strip().endswith(":"):
            continue
        k = ingredient_key(l)
        if k:
            groups.setdefault(k, []).append(i)
    # The other tool appended its copies after the original list: the
    # trailing run of lines that each repeat an earlier ingredient.
    appended: set[int] = set()
    first_seen: dict[frozenset, int] = {k: v[0] for k, v in groups.items()}
    for i in range(len(lines) - 1, -1, -1):
        k = ingredient_key(lines[i]) if not lines[i].strip().endswith(":") else frozenset()
        if not lines[i].strip():
            continue
        if k and first_seen.get(k, i) < i:
            appended.add(i)
        else:
            break
    drop: set[int] = set()
    for key, idx in groups.items():
        if len(idx) < 2:
            continue
        # A line that names its own use is a separate use; and the library's
        # substitutions are deliberate.
        uses = {i: use_of(lines[i]) for i in idx}
        plain = [i for i in idx if not uses[i] and not SUBST.search(lines[i])]
        hits = [l for l in src_lines if ingredient_key(l) == key]
        before = " | ".join(lines[i] for i in idx)

        if len(hits) == 1 and len(plain) >= 2 and all(i in appended for i in plain[1:]):
            keep, copies = plain[0], plain[1:]
            new_line = out[keep]
            if factor and split_amount(TAG_RE.sub("", hits[0]))[0]:
                t = target_amount(hits[0], factor, split_amount(TAG_RE.sub("", out[keep]))[0])
                if not t:
                    # e.g. "1-2 tablespoons": can't tell which copy is right.
                    report.append(["left, website amount is a range", before, "", hits[0]])
                    continue
                new_line = set_amount(out[keep], *t)
            have = " ".join(notes_of(new_line)).lower()
            for c in copies:
                for n in notes_of(lines[c]):
                    if n.lower() not in have and n.lower() not in ("optional",):
                        body = TAG_RE.sub("", new_line)
                        new_line = f"{body.rstrip()} ({n}){new_line[len(body):]}"
                        have += " " + n.lower()
            out[keep] = new_line
            drop.update(copies)
            report.append(["merged to website amount", before, new_line, hits[0]])
        elif len(hits) >= 2 and len(hits) == len(idx) and factor:
            changed = []
            for i, h in zip(idx, hits):
                t = target_amount(h, factor, split_amount(TAG_RE.sub("", out[i]))[0])
                line = set_amount(out[i], *t) if t else out[i]
                hu = use_of(h)
                if hu and not use_of(line):
                    body = TAG_RE.sub("", line)
                    line = f"{body.rstrip()} ({hu}){line[len(body):]}"
                out[i] = line
                changed.append(line)
            labelled = all(use_of(l) for l in changed)
            report.append(["set to website amounts" + ("" if labelled else ", no use label on website"),
                           before, " | ".join(changed), " | ".join(hits)])
        elif not hits and len(plain) >= 2 and all(i in appended for i in plain[1:]):
            drop.update(plain[1:])
            report.append(["appended copy dropped, not on website", before, out[plain[0]], ""])
        elif len(plain) >= 2 or (len(hits) >= 2 and len(hits) != len(idx)):
            report.append(["left, unclear", before, "", " | ".join(hits)])
    return [l for i, l in enumerate(out) if i not in drop], report


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", default=str(ROOT / "docs" / "genuine-repeats.tsv"))
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
    safe_print(f"Local pending recipes: {pending}")
    if pending and args.apply:
        raise SystemExit("not applied")
    with open(args.repeats, encoding="utf-8") as f:
        targets = {by_name[r[0]] for r in list(csv.reader(f, delimiter="\t"))[1:] if r[0] in by_name}

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
        for uid, src_url in sorted(targets):
            src_lines, src_yield = [], None
            if src_url:
                try:
                    src_lines, src_yield = fetch_source(src_url)
                except Exception:  # noqa: BLE001
                    pass
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            recipe = (body or {}).get("result")
            if st != 200 or not recipe:
                continue
            serv = servings_number(recipe.get("servings"))
            factor = serv / src_yield if serv and src_yield else None
            lines = (recipe.get("ingredients") or "").split("\n")
            new, rep = rationalise(lines, src_lines, factor)
            for r in rep:
                report.append([uid, recipe["name"], *r])
            if new == lines or not args.apply:
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
        w.writerow(["uid", "recipe", "kind", "lines before", "after", "website"])
        w.writerows(report)
    kinds: dict[str, int] = {}
    for r in report:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    safe_print(f"Report: {kinds}" + (f"; recipes written: {written}" if args.apply else " (dry-run)"))


if __name__ == "__main__":
    asyncio.run(main())
