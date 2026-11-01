"""Audit recipes against docs/recipe-audit-checklist.md (one pass per recipe).

Dry run (default): for each recipe in the batch, read the cloud copy and its
website (ingredients, sections, servings, notes) and report every issue,
the proposed fixes, and items needing the user's call. Writes
docs/audit-reports/DATE-BATCH.md and a TSV of proposed changes.

Apply: --apply-file TSV applies only the rows in that TSV (edit it down to
the approved rows), then rescores, writes through the sync API with a
backup, notifies, reads back and logs to docs/recipe-audit-log.tsv.

  python scripts/tools/audit_recipes.py --batch favourites
  python scripts/tools/audit_recipes.py --batch red --limit 10
  python scripts/tools/audit_recipes.py --apply-file docs/audit-reports/2026-10-13-favourites.tsv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
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

import cholesterol_score_lib as chol  # noqa: E402
import fodmap_score_lib as fodmap  # noqa: E402
from cleanup_library import (  # noqa: E402
    LOCAL_DB, TAG_RE, amount_value, broken_line, calc_hash, fetch_source, format_line,
    split_amount, tokens, typo_rewrite,
)
from dedupe_ingredients import dedupe, ingredient_key  # noqa: E402
from fix_dual_amount_lines import rewrite as dual_rewrite  # noqa: E402
from fix_portions import page_yield  # noqa: E402
from fix_qualifier_first_lines import rewrite as qualifier_rewrite  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from rationalise_repeats import set_amount, target_amount  # noqa: E402
from scan_site_disclaimers import PHRASES, page_text  # noqa: E402
from source_groups import fetch_groups  # noqa: E402

REPORTS = ROOT / "docs" / "audit-reports"
LOG = ROOT / "docs" / "recipe-audit-log.tsv"
OPEN = ROOT / "docs" / "audit-open-calls.tsv"  # decisions still waiting for the user
ALLIUM = re.compile(r"\b(leeks?|spring onions?|scallions?|green onions?)\b", re.I)
GREEN = re.compile(r"green\s+(?:tops?|parts?|leaves|ends?)|greens?\s+only|dark\s+green|tops\s+only", re.I)
DELIBERATE = re.compile(r"^(?:Low FODMAP|Changed):", re.M)
PROSE = re.compile(r"^(?:if you|in the original|you can|note|tip)\b|\b(?:if you|otherwise|I really)\b", re.I)
ODD_AMOUNT = re.compile(r"^\s*(?:\d+/\d+\s+[½¼¾⅓⅔⅛]|\S*/\d+th\b|1\s+slices\b|\d+\s+(?:gr|gram)\s+\d)", re.I)
GARLIC_EATEN = re.compile(r"\bcloves?\b.*\bgarlic\b|\bgarlic\s+cloves?\b", re.I)
GARLIC_OK = re.compile(r"infus|to make .*oil|omit|don.?t use|if you do not eat|remove|discard", re.I)
HOST_LAST: dict[str, float] = {}
VARIANT = re.compile(
    r"(?:to\s+make\s+(?:this|it)(?:\s+recipe)?\s+low[-\s]fodmap|for\s+(?:a\s+)?low[-\s]fodmap(?:\s+version)?)"
    r"[^.]{0,30}?\b(?:omit|swap|sub|replace|use|reduce|keep|skip|leave)\b", re.I)
DISCLAIMER = re.compile(
    r"(?:this|the)\s+recipe\s+is\s+not\s+(?:suitable\s+for\s+the\s+elimination|low[-\s]fodmap)|"
    r"don.?t\s+advise\s+making\s+this\s+recipe", re.I)


def folders(con, uid):
    cats = folders.cats
    out = []
    for (cu,) in con.execute("select category_uid from recipes_to_categories where recipe_uid=?", (uid,)):
        if cu not in cats:
            continue
        n, p = cats[cu]
        chain = [n]
        while p in cats:
            chain.append(cats[p][0])
            p = cats[p][1]
        out.append(" > ".join(reversed(chain)))
    return out


def polite(url: str) -> None:
    host = urlparse(url).netloc
    wait = 1.0 - (time.time() - HOST_LAST.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    HOST_LAST[host] = time.time()


def site_info(url: str) -> dict:
    """Website ingredients (with sections), servings and notes sentences."""
    info = {"lines": [], "groups": [], "yield": None, "sentences": [], "error": ""}
    if not url.startswith("http"):
        info["error"] = "no website"
        return info
    try:
        polite(url)
        info["lines"], info["yield"] = fetch_source(url)
        polite(url)
        info["groups"] = [(h, format_line(t)) for h, t in fetch_groups(url)]
        polite(url)
        text = page_text(url)
        info["sentences"] = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 10 < len(s) < 400]
        if not info["yield"]:
            polite(url)
            info["yield"] = page_yield(url)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"website unreadable: {str(e)[:60]}"
    return info


def audit_one(r: dict, src_url: str, in_low: bool, info: dict):
    """Returns (issues, proposals, user_calls). A proposal is
    (kind, before, after); before '' + after 'NOTE: …' adds a notes line;
    kind 'servings' sets servings."""
    issues, props, calls = [], [], []
    lines = (r.get("ingredients") or "").split("\n")
    notes = r.get("notes") or ""
    directions = r.get("directions") or ""
    serv = fodmap.parse_servings(r.get("servings"))

    # 1. website and portions
    if info["error"]:
        issues.append(info["error"])
    y = info["yield"]
    src_lines = [format_line(l) for l in info["lines"]]
    if y and src_lines:
        if abs(serv - y) > 0.01:
            issues.append(f"servings {r.get('servings')} vs website {y}")
        factor = serv / y
        for l in lines:
            body = TAG_RE.sub("", l)
            amt, rest = split_amount(body)
            if not amt or l.strip().endswith(":"):
                continue
            ot = tokens(rest)
            def jac(s):
                st = tokens(split_amount(s)[1])
                return len(ot & st) / len(ot | st) if ot and st else 0.0
            best = max(src_lines, key=jac, default=None)
            if not best or jac(best) < 0.5:
                continue
            # fresh vs ground/dried is a different ingredient ("fresh coriander" / "ground coriander")
            forms = lambda s: set(re.findall(r"\b(fresh|ground|dried)\b", s.lower()))
            if forms(body) != forms(best) and (forms(body) or forms(best)) and not forms(body) & forms(best):
                continue
            mv, sv = amount_value(amt), amount_value(split_amount(best)[0] or "x")
            if not (mv and sv and mv[0] == sv[0] and sv[1]):
                continue
            ratio = mv[1] / (sv[1] * factor)
            if 0.75 <= ratio <= 1.33:
                continue
            if mv[0] == "vol" and mv[1] <= 2.5 and sv[1] * factor <= 2.5:
                continue  # pinch-sized seasoning: rounding, not a portion error
            if ratio > 8 or ratio < 0.125:
                calls.append(f"amount far from the website's ({ratio:.2f}x), check it's the same ingredient: "
                             f"'{body.strip()}' vs '{best}'")
                continue
            word = next(iter(sorted(ot, key=len, reverse=True)), "")
            if DELIBERATE.search(notes) and word and word in notes.lower():
                continue  # noted deliberate change
            t = target_amount(best, factor, amt)
            if t:
                new = set_amount(l, *t)
                issues.append(f"portion: '{body.strip()}' is x{ratio:.2f} the website's per serving ({best})")
                props.append(("portion", l, new))

    # 2-3. website notes: actionable low FODMAP variants and recipe-level
    # disclaimers (generic "to make this low FODMAP X, simply: Step 1" and
    # ingredient asides like "honey is not suitable" are not flagged)
    for s in info["sentences"]:
        if VARIANT.search(s):
            if "per the author's notes" not in notes:
                calls.append(f"website low FODMAP variant, check it is transcribed: \"{s}\"")
        elif DISCLAIMER.search(s) and s[:40] not in notes:
            calls.append(f"website disclaimer, choose trash / adapt with a note / keep with warning: \"{s}\"")
            props.append(("disclaimer note", "", f"NOTE: Author's note: \"{s}\""))

    # 4. format
    for l in lines:
        n = l
        for f in (qualifier_rewrite, dual_rewrite, typo_rewrite):
            x = f(n)
            if x is not None:
                n = x
        if n != l:
            issues.append(f"format: '{TAG_RE.sub('', l).strip()}'")
            props.append(("format", l, n))
        if ODD_AMOUNT.search(l) or broken_line(TAG_RE.sub("", l)):
            calls.append(f"odd amount, needs the website's figure: '{TAG_RE.sub('', l).strip()}'")

    # 5. duplicates
    new_lines, removed, repeats = dedupe(lines)
    for kept, gone in removed:
        issues.append(f"duplicate: '{TAG_RE.sub('', gone).strip()}' copies '{TAG_RE.sub('', kept).strip()}'")
        props.append(("duplicate", gone, "DELETE"))
    for a, b in repeats:
        if not (re.search(r"\(for ", a) and re.search(r"\(for ", b)):
            calls.append(f"same ingredient twice, label each use from the website's sections: '{TAG_RE.sub('', a).strip()}' / '{TAG_RE.sub('', b).strip()}'")

    # 7. green parts
    site_text = " ".join(info["sentences"]) + " " + " ".join(t for _, t in info["groups"])
    for l in lines:
        body = TAG_RE.sub("", l)
        if ALLIUM.search(body) and not GREEN.search(body) and not re.search("infus", body, re.I):
            meth = any(GREEN.search(x) for x in directions.split("\n") if ALLIUM.search(x))
            site = bool(GREEN.search(site_text))
            if meth or site:
                after = re.sub(r"\s*$", "", body.rstrip()) + ", green tops only" + l[len(body):]
                issues.append(f"green part: '{body.strip()}' (the {'method' if meth else 'website'} uses the green part)")
                props.append(("green tops", l, after))
            else:
                calls.append(f"leek/spring onion with no green-part guidance: '{body.strip()}'")

    # 8. garlic eaten
    for l in lines:
        if GARLIC_EATEN.search(l) and not GARLIC_OK.search(l + " " + directions):
            calls.append(f"garlic eaten in the dish (swap for garlic-infused olive oil?): '{TAG_RE.sub('', l).strip()}'")

    # 9. unread amounts with a FODMAP limit
    for l in lines:
        body = fodmap.strip_inline_fodmap(fodmap.split_chol_suffix(l)[0])
        if not body.strip() or body.strip().endswith(":"):
            continue
        food = fodmap.match_food(body)
        if food and food.ftype and not food.no_upper_limit and fodmap.score_ingredient_line(l) is None:
            calls.append(f"amount the scorer can't read (has a FODMAP limit): '{body.strip()}'")

    # 10. prose lines
    for l in lines:
        b = l.strip()
        if len(b) > 50 and not split_amount(b)[0] and PROSE.search(b):
            issues.append(f"prose in the ingredient list: '{b[:80]}'")
            props.append(("prose to notes", l, "DELETE"))
            props.append(("prose to notes", "", f"NOTE: {b}"))

    # 12. rescore
    scope = "" if in_low else " (not in Low Fodmap: score for information)"
    t = fodmap.transform_recipe(r["name"], r.get("ingredients") or "", r.get("description") or "",
                                notes, servings=r.get("servings"))
    t2 = chol.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=r.get("servings"))
    if t2["ingredients"] != r.get("ingredients") or t2["name"] != r["name"]:
        a, b = r["name"].split(" ")[0], t2["name"].split(" ")[0]
        issues.append((f"rescore: marker {a} -> {b}" if a != b else f"rescore: tags updated, marker stays {a}") + scope)
        props.append(("rescore", "", "RESCORE"))
    if in_low and t2["name"].startswith("❌"):
        drv = [TAG_RE.sub("", l).strip() for l in t2["ingredients"].split("\n") if "🔴" in l]
        calls.append("high FODMAP in the Low Fodmap folder (rule 5): adapt with a note, or trash. "
                     + ("Drivers: " + "; ".join(drv) if drv else "Moderate ingredients add up."))
    return issues, props, calls


async def run_dry(args) -> None:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    folders.cats = {u: (n, p) for u, n, p in con.execute("select uid, name, parent_uid from recipe_categories")}
    logged = set()
    if LOG.exists():
        for row in list(csv.reader(LOG.open(encoding="utf-8"), delimiter="\t"))[1:]:
            if row and row[3] == str(args.version):
                logged.add(row[0])
    open_calls: dict[str, list] = {}
    if OPEN.exists():
        for row in list(csv.reader(OPEN.open(encoding="utf-8"), delimiter="\t"))[1:]:
            open_calls.setdefault(row[0], []).append(row)
    rows = con.execute("select uid, name, coalesce(source_url,'') from recipes where coalesce(in_trash,0)=0 order by name").fetchall()
    chosen = []
    for uid, name, url in rows:
        fs = folders(con, uid)
        in_low = any(f.lower().startswith("low fodmap") for f in fs)
        if args.batch == "favourites" and not any("favourite" in f.lower() for f in fs):
            continue
        if args.batch == "red" and not (in_low and name.startswith("❌")):
            continue
        if args.uids:
            if uid in args.uids:
                chosen.append((uid, name, url, in_low, fs))
            continue
        if uid in logged and uid not in open_calls:
            continue  # done at this checklist version, nothing waiting on the user
        chosen.append((uid, name, url, in_low, fs))
    con.close()
    if args.batch == "repeats":
        rep = ROOT / "docs" / "repeats-still-left.tsv"
        names = {r[0] for r in list(csv.reader(rep.open(encoding="utf-8"), delimiter="\t"))[1:]} if rep.exists() else set()
        chosen = [c for c in chosen if c[1] in names]
    if args.limit:
        chosen = chosen[: args.limit]

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.date.today().isoformat()}-{args.batch}"
    md, tsv = [f"# Recipe audit: {args.batch} ({datetime.date.today().isoformat()}, checklist v{args.version})", ""], []
    counts = {"recipes": 0, "clean": 0, "with fixes": 0, "need user call": 0, "proposals": 0}
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for uid, name, url, in_low, fs in chosen:
            st, body = await api_json(session, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers)
            r = (body or {}).get("result")
            if not r:
                continue
            if "waitrose.com" in url:
                # "Waitrose X" is the bought meal; "Low FODMAP X" is the user's
                # from-scratch version that cites the same page. Neither has
                # a recipe there to check portions against, and the pair is
                # intentional, not a duplicate.
                kind = "bought ready meal" if "waitrose" in r["name"].lower() else "from-scratch version of a Waitrose meal"
                info = {"lines": [], "groups": [], "yield": None, "sentences": [], "error": f"{kind}: no website reference"}
            else:
                info = site_info(url)
            issues, props, calls = audit_one(r, url, in_low, info)
            counts["recipes"] += 1
            counts["clean"] += not (issues or calls)
            counts["with fixes"] += bool(props)
            counts["need user call"] += bool(calls)
            counts["proposals"] += len(props)
            md += [f"## {r['name']}", f"- Folder: {'; '.join(fs) or 'none'} · servings {r.get('servings')} · {url or 'no website'}"]
            md += [f"- Issue: {i}" for i in issues] or ["- No issues found."]
            md += [f"- Proposed: {k}: `{b.strip() or '(add)'}` → `{a}`" for k, b, a in props]
            md += [f"- **Your call:** {c}" for c in calls]
            md.append("")
            for k, b, a in props:
                tsv.append([uid, r["name"], k, b, a])
            open_calls[uid] = [[uid, r["name"], datetime.date.today().isoformat(), c] for c in calls]
            safe_print(f"{counts['recipes']}/{len(chosen)} {r['name'][:60]}: {len(issues)} issues, {len(calls)} calls")
    with open(OPEN, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["uid", "recipe", "date", "decision needed"])
        for calls_ in open_calls.values():
            w.writerows(calls_)
    (REPORTS / f"{stamp}.md").write_text("\n".join(md), encoding="utf-8")
    with open(REPORTS / f"{stamp}.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["uid", "recipe", "kind", "before", "after"])
        w.writerows(tsv)
    safe_print(f"{counts} -> {REPORTS / (stamp + '.md')}")


async def run_apply(args) -> None:
    rows = list(csv.reader(open(args.apply_file, encoding="utf-8"), delimiter="\t"))[1:]
    by_uid: dict[str, list] = {}
    for uid, name, kind, before, after in rows:
        by_uid.setdefault(uid, []).append((kind, before, after))
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    pending = con.execute(
        "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
    ).fetchone()[0]
    con.close()
    if pending:
        raise SystemExit("Local SQLite has pending recipes; not applied.")
    backup = open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8")
    new_log = not LOG.exists()
    log = open(LOG, "a", encoding="utf-8", newline="")
    lw = csv.writer(log, delimiter="\t")
    if new_log:
        lw.writerow(["uid", "title", "date", "version", "issues", "fixes"])
    user, pw = paprika_credentials()
    lim = RateLimiter(0.5)
    written = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for uid, ops in by_uid.items():
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            r = body["result"]
            lines = r["ingredients"].split("\n")
            notes = r.get("notes") or ""
            missing = []
            for kind, before, after in ops:
                if after.startswith("NOTE: "):
                    if after[6:] not in notes:
                        notes = notes.rstrip() + "\n" + after[6:]
                elif after == "RESCORE":
                    continue
                elif before in lines:
                    i = lines.index(before)
                    if after == "DELETE":
                        del lines[i]
                    else:
                        lines[i] = after
                else:
                    missing.append(before)
            if missing:
                safe_print(f"skip {r['name']}: lines changed since the audit: {missing[:2]}")
                continue
            backup.write(json.dumps({k: r.get(k) for k in ("uid", "name", "servings", "ingredients", "notes", "description")},
                                    ensure_ascii=False) + "\n")
            t = fodmap.transform_recipe(r["name"], "\n".join(lines), r.get("description") or "", notes, servings=r.get("servings"))
            t2 = chol.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=r.get("servings"))
            upd = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], notes=notes)
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with session.post(url, headers=headers, data=form) as resp:
                if '"result":true' not in (await resp.text()).replace(" ", ""):
                    raise SystemExit(f"POST failed for {r['name']}")
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            if body["result"]["ingredients"] != upd["ingredients"]:
                raise SystemExit(f"read-back mismatch for {r['name']}")
            lw.writerow([uid, upd["name"], datetime.date.today().isoformat(), args.version,
                         len(ops), "; ".join(sorted({k for k, _, _ in ops}))])
            written += 1
        if written:
            await lim.wait_turn()
            async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as resp:
                safe_print("notify", resp.status)
    backup.close()
    log.close()
    safe_print(f"written {written} of {len(by_uid)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", choices=("favourites", "red", "repeats", "all"), default="favourites")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--uids-file", help="audit exactly these recipe uids (one per line), even if already logged")
    ap.add_argument("--apply-file")
    ap.add_argument("--version", default="4")
    args = ap.parse_args()
    args.uids = ({l.strip() for l in open(args.uids_file, encoding="utf-8") if l.strip()}
                 if args.uids_file else set())
    asyncio.run(run_apply(args) if args.apply_file else run_dry(args))


if __name__ == "__main__":
    main()
