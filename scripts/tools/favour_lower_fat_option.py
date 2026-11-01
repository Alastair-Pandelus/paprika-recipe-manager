"""Rule 7 with the user's preference (21 Oct 2026): where an ingredient line offers a
choice ("sour cream or Greek yogurt", "butter or olive oil"), the lower saturated-fat
option goes first and is the one scored; the others go in brackets:
  "2 tbsp lactose-free sour cream or Greek yogurt, for serving"
    -> "2 tbsp Greek yogurt (or lactose-free sour cream), for serving"
Only lines whose options score differently for saturated fat change, and never when the
leaner option scores worse for FODMAPs (those are listed). Adds a "Changed:" note and
rescores. Dry run unless --apply.

  python scripts/tools/favour_lower_fat_option.py --report docs/lower-fat-options.tsv [--apply]
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
from cleanup_library import LOCAL_DB, TAG_RE, calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from rescore_small_units import rescore  # noqa: E402

NOTE = "Changed: where a line offered a choice, the lower saturated-fat option now comes first and is the one scored; the others are in brackets."
AMOUNT = re.compile(r"^(\s*[\d½¼¾⅓⅔⅛][\d½¼¾⅓⅔⅛/.,\s-]*(?:\s*(?:g|kg|ml|l|tbsp|tsp|tablespoons?|teaspoons?|cups?|oz|lb|"
                    r"pinch|splash|knob|handful|portion|cloves?|slices?)\b\.?)?\s+)(.+)$", re.I)
TAIL = re.compile(r"(,\s*.*|\s+(?:for|to)\s+(?:serving|serve|garnish|frying|greasing|the [\w ]+|drizzling|topping|cooking)\b.*)$", re.I)


def sat_per_100(text: str) -> float | None:
    food = c.match_sat_food(text)
    return float(food["sat_fat_g_per_100g"]) if food and food.get("sat_fat_g_per_100g") is not None else None


def fodmap_ratio(line: str) -> float:
    sc = f.score_ingredient_line(line)
    if not sc or not sc[2]:
        return 0.0
    return sc[1] / sc[2]


def rewrite(line: str) -> tuple[str | None, str]:
    body = TAG_RE.sub("", line).strip()
    m = AMOUNT.match(body)
    if not m or re.search(r"\[recipe:", body):
        return None, ""
    amt, rest = m.group(1), m.group(2)
    if "(" in rest:  # keep anything already bracketed as it is
        head, bracket = rest.split("(", 1)
        bracket = " (" + bracket
    else:
        head, bracket = rest, ""
    if not re.search(r"\s+or\s+", head, re.I) or re.search(r"[:+]", head):
        return None, ""  # "fat: drippings, butter or oil" and "egg + water or milk" need a person
    tail = ""
    t = TAIL.search(head.split(" or ")[-1])
    if t:
        tail = t.group(1)
        head = head[: len(head) - len(tail)]
    opts = [re.sub(r"(?i)^(?:a|an|some)\s+", "", o.strip(" ,")) for o in re.split(r",?\s+or\s+", head, flags=re.I) if o.strip(" ,")]
    if len(opts) < 2 or any(len(o) > 45 or re.match(r"(?i)^(\d|another\b|extra\b)", o) for o in opts):
        return None, ""
    sats = [sat_per_100(o) for o in opts]
    if any(s is None for s in sats) or len(set(sats)) == 1:
        return None, ""
    best = min(range(len(opts)), key=lambda i: sats[i])
    if best == 0:
        return None, ""  # leanest is already first: scored right
    others = [o for i, o in enumerate(opts) if i != best]
    new = f"{amt}{opts[best]} (or {', or '.join(others)}){tail}{bracket}".replace("  ", " ")
    if fodmap_ratio(new) > fodmap_ratio(body) + 1e-9:
        return None, f"leaner option is higher in FODMAPs: {opts[best]}"
    return new, ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    plan, rep = {}, []
    for uid, name, ing in con.execute("select uid, name, ingredients from recipes where coalesce(in_trash,0)=0"):
        reps = {}
        for l in (ing or "").split("\n"):
            new, why = rewrite(l)
            if new:
                reps[l] = new
                rep.append([name, TAG_RE.sub("", l).strip(), new, ""])
            elif why:
                rep.append([name, TAG_RE.sub("", l).strip(), "", why])
        if reps:
            plan[uid] = reps
    with open(a.report, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t"); w.writerow(["recipe", "before", "after", "note"]); w.writerows(rep)
    print(f"{sum(len(v) for v in plan.values())} lines in {len(plan)} recipes; "
          f"{sum(1 for r in rep if r[3])} left (FODMAP); report -> {a.report}")
    if not a.apply:
        return
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    renamed = {}
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}

        async def write(r, upd):
            with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "ingredients", "notes", "description")}, ensure_ascii=False) + "\n")
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

        for uid, reps in plan.items():
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            r = b["result"]
            lines = [reps.get(l, l) for l in r["ingredients"].split("\n")]
            notes = r.get("notes") or ""
            if NOTE not in notes:
                notes = notes.rstrip() + "\n" + NOTE
            t2 = rescore(dict(r, ingredients="\n".join(lines), notes=notes))
            if t2["name"] != r["name"]:
                renamed[r["name"]] = t2["name"]
            await write(r, dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], notes=notes))
        linkers = [u for u, i in con.execute("select uid, ingredients from recipes where coalesce(in_trash,0)=0")
                   if any(f"[recipe:{o}]" in (i or "") for o in renamed)]
        for uid in linkers:
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            r = b["result"]
            ing = r["ingredients"]
            for o, n in renamed.items():
                ing = ing.replace(f"[recipe:{o}]", f"[recipe:{n}]")
            if ing != r["ingredients"]:
                await write(r, dict(r, ingredients=ing))
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
            print("notify", resp.status)
    (ROOT / "docs" / "lower-fat-options-renamed.json").write_text(json.dumps(renamed, ensure_ascii=False, indent=1), encoding="utf-8")
    print("titles changed", len(renamed))


if __name__ == "__main__":
    asyncio.run(main())
