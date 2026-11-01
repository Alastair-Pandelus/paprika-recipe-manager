"""Rescore recipes after the scorer learned the small units (21 Oct 2026): a pinch or dash
is 0.3 g, a splash 5 ml, a portion / handful / knob / sprig is not scored, and "slices of"
fruit or veg are 10 g each. Before, "1 pinch red pepper flakes" scored as a whole pepper
and "1 portion avocado" as a whole avocado. Rescores FODMAP and saturated fat for every
recipe with such a line, and updates links to any recipe whose title markers change.
Dry run unless --apply.

  python scripts/tools/rescore_small_units.py --report docs/rescore-small-units.tsv [--apply]
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

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from cleanup_library import LOCAL_DB, calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402

UNITS = re.compile(r"(?im)^\s*[\d½¼¾⅓⅔⅛][\d\s/.,-]*\s*(pinch|dash|splash|portion|handful|knob|sprinkle|squeeze|sprig|slices?\s+of)", re.I)


def rescore(r: dict) -> dict:
    notes = r.get("notes") or ""
    t = f.transform_recipe(r["name"], r["ingredients"], r.get("description") or "", notes, servings=r.get("servings"))
    t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=r.get("servings"))
    return t2


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    rows = [dict(uid=u, name=n, ingredients=i or "", description=d, notes=no, servings=s)
            for u, n, i, d, no, s in con.execute("select uid, name, ingredients, description, notes, servings from recipes "
                                                  "where coalesce(in_trash,0)=0")]
    todo, rep = [], []
    for r in rows:
        if not UNITS.search(r["ingredients"]):
            continue
        t2 = rescore(r)
        if t2["ingredients"] != r["ingredients"] or t2["name"] != r["name"] or (t2["description"] or "") != (r["description"] or ""):
            todo.append(r["uid"])
            rep.append([r["name"], t2["name"] if t2["name"] != r["name"] else ""])
    with open(a.report, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t"); w.writerow(["recipe", "new title"]); w.writerows(rep)
    print(f"{len(todo)} recipes to rescore, {sum(1 for x in rep if x[1])} title changes; report -> {a.report}")
    for x in rep:
        if x[1]:
            print("  ", x[0], "->", x[1])
    if not a.apply:
        return
    renamed = {}
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}

        async def write(r, upd):
            with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "ingredients", "description")}, ensure_ascii=False) + "\n")
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            url = f"{PAPRIKA_API}/v2/sync/recipe/{r['uid']}/"
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), r["name"]
            st, b = await api_json(s, lim, "GET", url, headers=h)
            assert b["result"]["ingredients"] == upd["ingredients"], r["name"]

        for uid in todo:
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            r = b["result"]
            t2 = rescore(r)
            if t2["name"] != r["name"]:
                renamed[r["name"]] = t2["name"]
            await write(r, dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"]))
        # recipe links follow the exact title, markers included
        linkers = [u for u, i in con.execute("select uid, ingredients from recipes where coalesce(in_trash,0)=0")
                   if any(f"[recipe:{old}]" in (i or "") for old in renamed)]
        for uid in linkers:
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            r = b["result"]
            ing = r["ingredients"]
            for old, new in renamed.items():
                ing = ing.replace(f"[recipe:{old}]", f"[recipe:{new}]")
            if ing != r["ingredients"]:
                await write(r, dict(r, ingredients=ing))
                print("link updated in", r["name"])
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
            print("notify", resp.status)
    print("rescored", len(todo), "titles changed", len(renamed))


if __name__ == "__main__":
    asyncio.run(main())
