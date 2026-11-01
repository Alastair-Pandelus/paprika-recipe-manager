"""Rescale recipes back to their source website's servings and amounts (cleanup_library.rebuild).

For the recipes listed in a review TSV (column "recipe"), each recipe is rebuilt to the
website's yield: lines the old rescale broke (the listed ones) take the source amount;
other lines are scaled up, or keep the source amount when they agree; deliberate changes
(a clear difference from the source) are scaled, not replaced. Wording and tags stay.
Dry run unless --apply; writes a before/after report.

  python scripts/tools/rescale_from_source.py --review docs/missing-units-review.tsv --report docs/rescale-from-source.tsv [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
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
from cleanup_library import LOCAL_DB, TAG_RE, calc_hash, fetch_source, rebuild  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-file", help="recipe names (one per line) to leave alone")
    a = ap.parse_args()
    skip = set(Path(a.skip_file).read_text(encoding="utf-8").split("\n")) if a.skip_file else set()
    review = list(csv.reader(open(a.review, encoding="utf-8"), delimiter="\t"))[1:]
    broken_by_name: dict[str, set[str]] = {}
    for r in review:
        broken_by_name.setdefault(r[0], set()).add(r[1])
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    targets = []
    for name in broken_by_name:
        if name in skip:
            continue
        row = con.execute("select uid, coalesce(source_url,'') from recipes where name=? and coalesce(in_trash,0)=0", (name,)).fetchone()
        if row:
            targets.append((row[0], name, row[1]))
    print(len(targets), "recipes")
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    rep, last = [], {}
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        for uid, name, src in targets:
            host = urlparse(src).netloc
            time.sleep(max(0, 1.0 - (time.time() - last.get(host, 0)))); last[host] = time.time()
            try:
                src_lines, y = fetch_source(src)
            except Exception as e:  # noqa: BLE001
                rep.append([name, "", "", f"fetch failed: {str(e)[:60]}"]); continue
            if not src_lines or not y:
                rep.append([name, "", "", "source has no ingredients or yield"]); continue
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, b = await api_json(s, lim, "GET", url, headers=h); r = b["result"]
            serv = f.parse_servings(r.get("servings")) or 1
            factor = y / serv
            old = r["ingredients"].split("\n")
            broken = {l for l in old if TAG_RE.sub("", l).strip() in broken_by_name[name]}
            new, notes = rebuild(old, src_lines, factor, broken, set())
            t = f.transform_recipe(r["name"], "\n".join(new), r.get("description") or "", r.get("notes") or "", servings=str(y))
            t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], r.get("notes") or "", servings=str(y))
            rep.append([name, f"servings {r.get('servings')} -> {y}", t2["name"] if t2["name"] != r["name"] else "", "; ".join(notes)[:300]])
            for o, n in zip([TAG_RE.sub('', x).strip() for x in old], [TAG_RE.sub('', x).strip() for x in t2['ingredients'].split('\n')]):
                if o != n:
                    rep.append(["", o, n, ""])
            if not a.apply:
                continue
            note_line = (f"Changed: rescaled to the website's {y} servings ({src}); amounts the old 1-serving rescale had broken "
                         "were taken from the website, deliberate changes kept.")
            notes_new = (r.get("notes") or "").rstrip() + ("\n" + note_line if note_line not in (r.get("notes") or "") else "")
            with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "servings", "ingredients", "notes")}, ensure_ascii=False) + "\n")
            upd = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], notes=notes_new, servings=str(y))
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData(); form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()), content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), name
            st, b = await api_json(s, lim, "GET", url, headers=h)
            assert b["result"]["ingredients"] == upd["ingredients"], name
        if a.apply:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
                print("notify", resp.status)
    with open(a.report, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t"); w.writerow(["recipe", "before", "after", "notes"]); w.writerows(rep)
    print("report ->", a.report)


if __name__ == "__main__":
    asyncio.run(main())
