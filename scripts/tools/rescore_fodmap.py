"""Rescore recipes' FODMAP tags and title marker with the current scorer.

Only the FODMAP marker in the title (✅/ℹ️/❌), the FODMAP tags on ingredient
lines and the FODMAP summary in the description change; the fat marker,
fat tags, notes and everything else stay as they are.

Before scoring, two data fixes from the 2026-10-13 clean-up:
- "35.3 oz kilo …" (a pack-size slip) goes back to "1 kg …".
- "(optional)" lines that the old 1-serving rescale never scaled are
  scaled by 1 / website yield in recipes still at 1 serving.

Through the sync API only; refuses while the local SQLite has pending
recipes; backs each recipe up first. Dry-run unless --apply.

  python scripts/tools/rescore_fodmap.py --scope karlijn --report r.tsv --backup b.jsonl [--apply]
  python scripts/tools/rescore_fodmap.py --scope all --report r.tsv --backup b.jsonl
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

import fodmap_score_lib as fodmap  # noqa: E402
from cleanup_library import LOCAL_DB, TAG_RE, calc_hash, fetch_source, format_line, scale_line  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

KILO_SLIP = re.compile(r"^35\.3 oz kilo\s+")


def never_scaled_lines() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    q = ROOT / "docs" / "qualifier-lines-applied.tsv"
    d = ROOT / "docs" / "dual-amount-applied.tsv"
    if q.exists():
        for _, uid, _, after in list(csv.reader(q.open(encoding="utf-8"), delimiter="\t"))[1:]:
            out.setdefault(uid, set()).add(after)
    if d.exists():
        for _, uid, before, after in list(csv.reader(d.open(encoding="utf-8"), delimiter="\t"))[1:]:
            if before in out.get(uid, set()):
                out[uid].add(after)
    return out


PER_SERVE = re.compile(r"per (?:person|serving|portion|bowl)|\beach\b", re.I)


def tidy_amount(line: str) -> str:
    """"37 1/2 g" -> "37.5 g"; "0 tsp" -> "1 pinch"."""
    m = re.match(r"^(\d+)(?: (\d+)/(\d+))?\s*(g|ml)\b", line)
    if m and m.group(2):
        v = int(m.group(1)) + int(m.group(2)) / int(m.group(3))
        line = f"{round(v, 1):g} {m.group(4)}" + line[m.end():]
    m = re.match(r"^(\d+)/(\d+)\s*(g|ml)\b", line)
    if m:
        line = f"{round(int(m.group(1)) / int(m.group(2)), 1):g} {m.group(3)}" + line[m.end():]
    return re.sub(r"^0 (?:tsp|tbsp|cups?|teaspoons?|tablespoons?)\b", "1 pinch", line)


def marker(name: str) -> str:
    return name.split(" ", 1)[0]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=("karlijn", "all"), required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    pending = con.execute(
        "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
    ).fetchone()[0]
    rows = con.execute(
        "select uid, source_url from recipes where coalesce(in_trash,0)=0"
    ).fetchall()
    con.close()
    safe_print(f"Local pending recipes: {pending}")
    if pending and args.apply:
        raise SystemExit("not applied")
    targets = [(u, s) for u, s in rows if args.scope == "all" or "karlijnskitchen" in (s or "")]
    unscaled = never_scaled_lines()

    report, written = [], 0
    backup = open(args.backup, "a", encoding="utf-8") if args.apply else None
    user, pw = paprika_credentials()
    lim = RateLimiter(0.45)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for uid, src in targets:
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            recipe = (body or {}).get("result")
            if st != 200 or not recipe:
                continue
            lines = (recipe.get("ingredients") or "").split("\n")
            fixed = list(lines)
            for i, l in enumerate(fixed):
                if KILO_SLIP.match(l):
                    fixed[i] = KILO_SLIP.sub("1 kg ", l)
                    report.append([uid, recipe["name"], "data: kilo slip", l, fixed[i]])
            todo = [i for i, l in enumerate(fixed) if l in unscaled.get(uid, set())
                    and re.match(r"^\s*[\d½¼¾⅓⅔⅛]", l) and not PER_SERVE.search(l)]
            if todo and str(recipe.get("servings")).strip() == "1" and src:
                try:
                    _, y = fetch_source(src)
                except Exception:  # noqa: BLE001
                    y = None
                if y and y > 1:
                    for i in todo:
                        body_ = TAG_RE.sub("", fixed[i])
                        s = scale_line(body_, 1 / y)
                        if s:
                            new_l = tidy_amount(format_line(s)) + fixed[i][len(body_):]
                            report.append([uid, recipe["name"], f"data: optional line scaled 1/{y}", fixed[i], new_l])
                            fixed[i] = new_l
            t = fodmap.transform_recipe(recipe["name"], "\n".join(fixed),
                                        recipe.get("description") or "", recipe.get("notes") or "",
                                        servings=recipe.get("servings"))
            new = dict(recipe, name=t["name"], ingredients=t["ingredients"], description=t["description"])
            if (new["name"], new["ingredients"], new["description"]) == (
                    recipe["name"], recipe.get("ingredients"), recipe.get("description")):
                continue
            if marker(new["name"]) != marker(recipe["name"]):
                drivers = [l for l in new["ingredients"].split("\n") if re.search("[🟠🔴]", l)]
                report.append([uid, recipe["name"], f"marker {marker(recipe['name'])} -> {marker(new['name'])}",
                               " | ".join(drivers), ""])
            else:
                report.append([uid, recipe["name"], "tags only", "", ""])
            if not args.apply:
                continue
            backup.write(json.dumps({"uid": uid, "name": recipe["name"], "ingredients": recipe.get("ingredients"),
                                     "description": recipe.get("description")}, ensure_ascii=False) + "\n")
            backup.flush()
            new["hash"] = calc_hash(new)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(new, separators=(",", ":")).encode()),
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
        w.writerow(["uid", "recipe", "kind", "detail", "after"])
        w.writerows(report)
    kinds: dict[str, int] = {}
    for r in report:
        k = r[2] if not r[2].startswith("data: optional") else "data: optional line scaled"
        kinds[k] = kinds.get(k, 0) + 1
    safe_print(f"Report: {kinds}" + (f"; recipes written: {written}" if args.apply else " (dry-run)"))


if __name__ == "__main__":
    asyncio.run(main())
