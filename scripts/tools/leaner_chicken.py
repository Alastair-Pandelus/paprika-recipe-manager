"""Swap chicken thigh for skinless chicken breast where the cut is not intrinsic to the
dish (the user, 21 Oct 2026). Left alone: dishes named for thighs, legs, drumsticks or
wings; bone-in or skin-on braises and roasts (the bone and skin are the point); stock
made from bones; ready-meal product lines (Waitrose); lines with no amount.
Boneless thigh lines become breast; "thighs or breasts" puts breast first; counts become
grams (a boneless thigh is about 100 g). The method follows, and a "Changed:" note says
breast cooks faster. Dry run unless --apply.

  python scripts/tools/leaner_chicken.py --report docs/leaner-chicken.tsv [--apply]
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

from cleanup_library import LOCAL_DB, TAG_RE, calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from rescore_small_units import rescore  # noqa: E402

INTRINSIC_TITLE = re.compile(r"(?i)thigh|drumstick|\blegs?\b|\bwings?\b|whole chicken|roast chicken|rotisserie|spatchcock|"
                             r"waitrose|broth|stock|cacciatore|chicken soup|stew|greek lemon chicken|portuguese|sheet pan chicken")
NOTE = ("Changed: skinless chicken breast instead of chicken thigh, to cut the saturated fat. Breast cooks faster and dries "
        "out sooner, so take it off the heat as soon as it is cooked through.")
NUM = r"(\d+(?:\s+\d+/\d+|/\d+|\.\d+)?|[½¼¾⅓⅔])"


def to_float(s: str) -> float:
    from fractions import Fraction
    s = s.replace("½", "1/2").replace("¼", "1/4").replace("¾", "3/4").replace("⅓", "1/3").replace("⅔", "2/3")
    return float(sum(Fraction(p) for p in s.split()))


def swap_line(body: str) -> str | None:
    low = body.lower()
    if not re.search(r"thigh", low) or not re.match(r"^\s*[\d½¼¾⅓⅔]", body):
        return None
    if re.search(r"bone-in|bone in|with bones?|skin-on|skin on|cutlets", low):
        return None
    # "breasts or thighs" / "breast or thigh": already breast first
    if re.search(r"breasts?\s+or\s+thighs?", low):
        return None
    # "thighs or breasts" / "thigh or breast" / "thighs (or breasts)": breast first
    m = re.search(r"(?i)chicken\s+thighs?\s*\(?\s*or\s+breasts?\s*\)?", body) or re.search(r"(?i)thighs?\s*\(?\s*or\s+breasts?\s*\)?", body)
    if m:
        return body[: m.start()] + "chicken breast (or thigh)" + body[m.end():] if "chicken" in m.group(0).lower() \
            else body[: m.start()] + "breast (or thigh)" + body[m.end():]
    # counts: "1 boneless, skinless chicken thighs", "2 large cooked chicken thighs"
    c = re.match(rf"^\s*{NUM}\s+(?:large\s+|medium\s+|small\s+)?((?:cooked\s+)?)(?:boneless,?\s*)?(?:skinless\s+)?chicken\s+thighs?\b(.*)$", body, re.I)
    if c:
        grams = round(to_float(c.group(1)) * 100)
        return f"{grams} g {c.group(2)}skinless chicken breast{c.group(3)}"
    new = re.sub(r"(?i)\(?skinless\s+chicken\s+thighs?\)?", "skinless chicken breast", body)
    new = re.sub(r"(?i)chicken\s+thighs?\s+fillets?", "skinless chicken breast fillet", new)
    new = re.sub(r"(?i)chicken\s+thighs?", "skinless chicken breast", new)
    new = re.sub(r"(?i)skinless\s+skinless", "skinless", new)
    return new if new != body else None


def swap_method(d: str) -> str:
    d = re.sub(r"(?i)chicken thighs?", "chicken breast", d)
    return re.sub(r"(?i)\bthighs\b", "chicken pieces", re.sub(r"(?i)\bthigh\b", "chicken", d))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    plan, rep = {}, []
    for uid, name, ing in con.execute("select uid, name, ingredients from recipes where coalesce(in_trash,0)=0"):
        if not re.search(r"(?i)thigh", ing or ""):
            continue
        if INTRINSIC_TITLE.search(name):
            rep.append([name, "", "", "left: the cut is part of the dish"])
            continue
        reps = {}
        for l in (ing or "").split("\n"):
            body = TAG_RE.sub("", l).strip()
            if "thigh" not in body.lower():
                continue
            new = swap_line(body)
            if new:
                reps[l] = new
                rep.append([name, body, new, ""])
            else:
                rep.append([name, body, "", "left: bone-in/skin-on, breast already first, or no amount"])
        if reps:
            plan[uid] = reps
    with open(a.report, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t"); w.writerow(["recipe", "before", "after", "note"]); w.writerows(rep)
    print(f"{sum(len(v) for v in plan.values())} lines in {len(plan)} recipes; report -> {a.report}")
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
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "ingredients", "directions", "notes", "description")},
                                    ensure_ascii=False) + "\n")
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
            await write(r, dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], notes=notes,
                                directions=swap_method(r.get("directions") or "")))
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
    (ROOT / "docs" / "leaner-chicken-renamed.json").write_text(json.dumps(renamed, ensure_ascii=False, indent=1), encoding="utf-8")
    print("titles changed", len(renamed))


if __name__ == "__main__":
    asyncio.run(main())
