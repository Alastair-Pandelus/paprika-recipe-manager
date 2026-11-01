"""Resolve remaining repeated ingredients from the website's sections.

For each recipe in --repeats (and the unlabelled pairs in --unlabelled), the
website's ingredient list is read with its section headings
(source_groups.fetch_groups):

- The website uses the ingredient in more than one section: each line takes
  the website's amount at the recipe's servings and is labelled with its
  section, e.g. "17 g granulated sugar (for the icing)".
- The website lists it once: the copies were appended by another tool, so one
  line stays at the website's amount (ranges scaled too) and the copies go.
- Otherwise the lines are left and listed.

The library's substitutions ("replaces", spring onion greens, leek) are never
merged. Through the sync API only; refuses while the local SQLite has pending
recipes; backs each recipe up first. Dry-run unless --apply.

  python scripts/tools/resolve_repeats.py --report r.tsv --backup b.jsonl [--apply]
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

from cleanup_library import LOCAL_DB, NUM, TAG_RE, calc_hash, fetch_source, num, split_amount  # noqa: E402
from dedupe_ingredients import ingredient_key  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from cleanup_library import amount_value, format_line  # noqa: E402
from rationalise_repeats import SUBST, fmt_qty, servings_number, set_amount, target_amount, use_of  # noqa: E402
from source_groups import fetch_groups  # noqa: E402

RANGE = re.compile(rf"^\s*({NUM})\s*(?:-|–|to)\s*({NUM})\s*([a-z]+)?\b", re.I)


def amount_for(src_text: str, factor: float, like_line: str) -> tuple[str, float | None] | None:
    """The website's amount at the recipe's servings; ranges keep both ends."""
    m = RANGE.match(src_text)
    if m:
        a, b = num(m.group(1)), num(m.group(2))
        if a is None or b is None:
            return None
        unit = m.group(3) or ""
        lo = fmt_qty(a * factor, unit).rsplit(" ", 1)[0] if unit else fmt_qty(a * factor, "")
        return f"{lo} to {fmt_qty(b * factor, unit)}".strip(), None
    return target_amount(src_text, factor, split_amount(TAG_RE.sub("", like_line))[0])


NOT_A_SECTION = re.compile(r"nutrition|equipment|notes?|tips?|tools|instructions|method|what do you need|other ingredients", re.I)


def label_for(heading: str) -> str:
    """"For the cake" -> "for the cake"; "Salad Dressing" -> "for the salad
    dressing"; "To finish" stays "to finish". "" when it isn't a section."""
    h = re.sub(r"\([^)]*\)", "", heading).strip().rstrip(":").strip()
    h = re.sub(r"^ingredients\s+", "", h, flags=re.I)
    if not h or h.lower() in ("ingredients", "for the recipe") or NOT_A_SECTION.search(h):
        return ""
    h = h[0].lower() + h[1:]
    if re.match(r"(?:for|to)\s", h, re.I):
        return h.lower()
    return f"for the {h.lower()}"


def amount_of(line: str):
    a = split_amount(TAG_RE.sub("", line))[0]
    return amount_value(a) if a else None


def resolve(lines: list[str], groups: list[tuple[str, str]], factor: float | None
            ) -> tuple[list[str], list[list[str]]]:
    out = list(lines)
    drop: set[int] = set()
    report = []
    keyed: dict[frozenset, list[int]] = {}
    for i, l in enumerate(lines):
        if l.strip() and not l.strip().endswith(":") and not SUBST.search(l):
            k = ingredient_key(l)
            if k:
                keyed.setdefault(k, []).append(i)
    # The other tool appended its copies after the original list.
    appended: set[int] = set()
    first_at = {k: v[0] for k, v in keyed.items()}
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        k = ingredient_key(lines[i])
        if k and first_at.get(k, i) < i:
            appended.add(i)
        else:
            break
    for key, idx in keyed.items():
        if len(idx) < 2:
            continue
        hits = [(h, t) for h, t in groups if ingredient_key(t) == key]
        before = " | ".join(lines[i] for i in idx)
        if not hits:
            report.append(["left, not on website", before, "", ""])
            continue
        if not factor:
            report.append(["left, servings or website yield unknown", before, "", ""])
            continue
        labels = [label_for(h) for h, _ in hits]
        if len(hits) == 1:
            if any(use_of(lines[i]) for i in idx):
                report.append(["left, a line names its own use", before, "", hits[0][1]])
                continue
            # Merge only when one line already matches the website's amount
            # (within 2x); that line stays, the others were the appended copies.
            t = amount_for(hits[0][1], factor, out[idx[0]])
            wv = amount_of(f"{t[0]} x") if t else None
            ov = amount_of(lines[idx[0]])
            first_matches = bool(wv and ov and ov[0] == wv[0] and wv[1] and 0.5 <= ov[1] / wv[1] <= 2)
            if not first_matches or not all(i in appended for i in idx[1:]):
                report.append(["left, not a clear appended copy", before, "", hits[0][1]])
                continue
            keep = idx[0]
            drop.update(idx[1:])
            report.append(["merged, website lists once", before, out[keep], hits[0][1]])
        elif (len(hits) == len(idx) and sum(1 for l in labels if not l) <= 1
              and len({l for l in labels if l}) == sum(1 for l in labels if l)):
            # The main list often has no heading; its line stays unlabelled.
            done = []
            for i, (_, t_src), lab in zip(idx, hits, labels):
                t = amount_for(t_src, factor, out[i])
                line = set_amount(out[i], t[0], t[1]) if t and "/4th" not in out[i] else out[i]
                if lab and lab.lower() not in line.lower() and not use_of(line):
                    body = TAG_RE.sub("", line)
                    line = f"{body.rstrip()} ({lab}){line[len(body):]}"
                out[i] = line
                done.append(line)
            report.append(["labelled by website section", before, " | ".join(done),
                           " | ".join(f"{h}: {t}" for h, t in hits)])
        else:
            report.append(["left, website sections unclear", before, "",
                           " | ".join(f"{h}: {t}" for h, t in hits)])
    return [l for i, l in enumerate(out) if i not in drop], report


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", default=str(ROOT / "docs" / "repeats-left.tsv"))
    ap.add_argument("--unlabelled", default=str(ROOT / "docs" / "repeats-applied.tsv"))
    ap.add_argument("--report", required=True)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    pending = con.execute(
        "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
    ).fetchone()[0]
    rows = con.execute("select uid, name, source_url from recipes where coalesce(in_trash,0)=0").fetchall()
    con.close()
    uid_by_name = {n: u for u, n, _ in rows}
    src_by_uid = {u: s for u, _, s in rows}
    safe_print(f"Local pending recipes: {pending}")
    if pending and args.apply:
        raise SystemExit("not applied")
    targets: set[str] = set()
    with open(args.repeats, encoding="utf-8") as f:
        targets |= {uid_by_name[r[0]] for r in list(csv.reader(f, delimiter="\t"))[1:] if r[0] in uid_by_name}
    with open(args.unlabelled, encoding="utf-8") as f:
        targets |= {r[0] for r in list(csv.reader(f, delimiter="\t"))[1:] if "no use label" in r[2]}

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
        for uid in sorted(targets):
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            recipe = (body or {}).get("result")
            if st != 200 or not recipe:
                continue
            src_url = src_by_uid.get(uid)
            if not src_url:
                report.append([uid, recipe["name"], "left, no website", "", "", ""])
                continue
            try:
                groups = fetch_groups(src_url)
                jl, src_yield = fetch_source(src_url)
            except Exception as e:  # noqa: BLE001
                report.append([uid, recipe["name"], "left, website unreachable", str(e)[:80], "", ""])
                continue
            # "340 g ( 1 2/3 cups ) sugar" -> "340 g sugar", as the recipe lines are.
            groups = [(h, format_line(l)) for h, l in groups]
            if not any(ingredient_key(t) for _, t in groups):
                groups = [("", format_line(l)) for l in jl]
            serv = servings_number(recipe.get("servings"))
            factor = serv / src_yield if serv and src_yield else None
            lines = (recipe.get("ingredients") or "").split("\n")
            new, rep = resolve(lines, groups, factor)
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
