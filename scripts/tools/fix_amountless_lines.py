"""Give every ingredient line an amount first (rule 1), library-wide, for the lines the
earlier pass (fix_amount_first_lines.py) left: no amount at all, "Optional: …", "A pinch
of …", "Fresh basil, a small handful", salt and pepper. Uses the same amount rules as the
She Can't Eat What import (amountless()). Equipment lines (tins, pans, dishes) move to the
notes. Section headers stay. Lines with no confident amount are listed, not changed.
Rescores the recipe. Dry run unless --apply.

  python scripts/tools/fix_amountless_lines.py --report docs/amountless-lines.tsv [--apply]
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

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from cleanup_library import LOCAL_DB, TAG_RE, calc_hash  # noqa: E402
from fix_amount_first_lines import rewrite as amount_first  # noqa: E402
from import_shecanteatwhat_recipes import AMT, SERVE_AMOUNT, amountless  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402

EQUIPMENT = re.compile(r"\b(tin|pan|dish|tray|skillet|mould|mold|cutters?|skewers|ramekins?|cases|baking paper|parchment|"
                       r"cling film|foil|blender|processor|piping bag|thermometer|jar|loaf tin)\b", re.I)


def is_header(b: str) -> bool:
    if b.endswith(":") and len(b) < 60 and not re.match(r"(?i)optional", b):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z &'/-]{1,40}", b)) and not re.search(
        r"(?i)salt|pepper|oil|juice|water|parsley|basil|coriander|ice|lemon|lime|mint|chives|dill|rocket|spinach|butter|honey|"
        r"yog|cream|cheese|seeds|nuts|sugar|syrup|cinnamon|paprika|cumin|herbs|thyme|rosemary|sage|oregano|nutmeg|tamari|"
        r"vinegar|mustard|stock|milk|rice|bread|flour|eggs?|garnish|toppings?", b)


def fix(b: str) -> str | None:
    """New text for a line with no leading amount, or None."""
    s = html.unescape(b).strip()
    m = re.match(r"(?i)^optional(?:\s+(?:garnish|topping|toppings|extra|extras|for [^:]{1,30}))?\s*[:,-]\s*(.+)$", s)
    if m:
        inner = m.group(1).strip()
        new = inner if AMT.match(inner) else (amount_first(inner) or None)
        if not new or not AMT.match(new):
            new = amountless(inner)
        if not new:  # optional extras: a default amount by kind of ingredient
            amt = next((a for pat, a in SERVE_AMOUNT if re.search(pat, inner, re.I)), "1 portion")
            new = f"{amt} {inner[0].lower() + inner[1:]}"
        new = re.sub(r"\s*\((?:to serve|to taste)\)$", "", new) if "(optional" not in new else new
        return new if "optional" in new.lower() else f"{new} (optional)"
    m = re.match(r"^([A-Za-z][\w '-]{1,40}),\s*(?:a |an )?(small |large |big |generous )?(handful|bunch|pinch|splash|sprig|few sprigs|drizzle)\b(.*)$", s, re.I)
    if m:
        name, size, unit, rest = m.groups()
        return f"1 {(size or '').lower()}{unit.lower()} {name[0].lower() + name[1:]}{rest}".replace("  ", " ")
    new = amount_first(s)
    if new and AMT.match(new):
        return new
    m = re.match(r"(?i)^(tbsp|tsp|tablespoons?|teaspoons?|g|ml)\s+(.+)$", s)  # "tbsp olive oil": the number is missing
    if m:
        return None
    return amountless(s)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    plan: dict[str, tuple[str, dict[str, str], list[str]]] = {}
    rep = []
    for uid, name, ing in con.execute("select uid, name, ingredients from recipes where coalesce(in_trash,0)=0"):
        reps, equip = {}, []
        for l in (ing or "").split("\n"):
            b = TAG_RE.sub("", l).strip()
            if not b or AMT.match(b) or is_header(b):
                continue
            if EQUIPMENT.search(b) and not re.search(r"(?i)\b(of|for the)\b.*\b(oil|butter|cream)\b|greasing|to grease|for the (tin|pan)", b) \
                    and re.match(r"(?i)^(a|an|one|\d+\s*cm|\d+[\"”]|large|small|medium|square|round|loaf|muffin|cupcake|baking|8|9)\b", b):
                equip.append(b)
                rep.append([name, b, "", "equipment: moved to the notes"])
                continue
            new = fix(b)
            if new and new != b:
                reps[l] = new
                rep.append([name, b, new, ""])
            else:
                rep.append([name, b, "", "no confident amount: left as it is"])
        if reps or equip:
            plan[uid] = (name, reps, equip)
    with open(a.report, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t"); w.writerow(["recipe", "before", "after", "note"]); w.writerows(rep)
    print(f"{sum(len(p[1]) for p in plan.values())} lines fixed, {sum(len(p[2]) for p in plan.values())} equipment lines, "
          f"{sum(1 for r in rep if r[3].startswith('no'))} left, in {len(plan)} recipes; report -> {a.report}")
    if not a.apply:
        return
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    renamed = []
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        for i, (uid, (name, reps, equip)) in enumerate(plan.items(), 1):
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, b = await api_json(s, lim, "GET", url, headers=h)
            r = b["result"]
            lines = r["ingredients"].split("\n")
            new = [reps.get(l, l) for l in lines if TAG_RE.sub("", l).strip() not in equip]
            if new == lines:
                continue
            notes = r.get("notes") or ""
            if equip:
                notes = notes.rstrip() + "\nEquipment: " + "; ".join(equip) + "."
            t = f.transform_recipe(r["name"], "\n".join(new), r.get("description") or "", notes, servings=r.get("servings"))
            t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=r.get("servings"))
            with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "ingredients", "notes")}, ensure_ascii=False) + "\n")
            upd = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], notes=notes)
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), name
            st, b = await api_json(s, lim, "GET", url, headers=h)
            assert b["result"]["ingredients"] == upd["ingredients"], name
            if t2["name"] != r["name"]:
                renamed.append([r["name"], t2["name"]])
            if i % 50 == 0:
                print(i, "done", flush=True)
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
            print("notify", resp.status)
    (ROOT / "docs" / "amountless-renamed.json").write_text(json.dumps(renamed, ensure_ascii=False, indent=1), encoding="utf-8")
    print("markers changed:", len(renamed))


if __name__ == "__main__":
    asyncio.run(main())
