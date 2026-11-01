"""Fix duplicate / broken olive-oil lines left after garlic-oil → olive oil.

Conservative rules only:
1. Ingredients — olive-oil lines:
   - Drop exact duplicates (ignore sat-fat tags / (N ml) / LF notes).
   - Drop bare-unit lines ("tbsp olive oil") if any quantified oil line exists.
   - Drop broken amount-only lines ("1/4 olive oil") if quantified oil exists.
   - "olive OR olive oil" → "olive oil".
   - "… (or more olive oil)" near-dup of same amount → drop the "(or more)" line.
2. Ingredients — bare-unit echo lines (Monash artifact), oil or not:
   - Drop "tbsp mustard seeds" when "1/2 tsp mustard seeds" (or similar) exists.
   - Require the bare rest to equal the quantified food key (not a substring).
3. Directions: collapse "olive oil, olive oil" / "and" / "or" duplicates.

  python scripts/tools/fix_dup_olive_oil.py
  python scripts/tools/fix_dup_olive_oil.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import (  # noqa: E402
    RateLimiter,
    api_json,
    safe_print,
    save_recipe,
)

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

SAT_TAG = re.compile(r"\s*[♥💚♡❣💔].*$")
OIL = re.compile(r"olive\s+oil", re.I)
BARE_UNIT = re.compile(
    r"^(?P<unit>tbsp|tsp|tablespoons?|teaspoons?|cups?|ml|g)\b\.?\s*",
    re.I,
)
BROKEN_AMT_OIL = re.compile(
    r"^(?P<amt>\d+\s+\d/\d|\d/\d|\d+[.,]?\d*)\s+olive\s+oil\b",
    re.I,
)
HAS_UNIT = re.compile(
    r"\b(tsp|tbsp|teaspoon|tablespoon|cup|ml|g|grams?|litre|liter|l)\b",
    re.I,
)
PURPOSE = re.compile(
    r"\b(for the|for serving|to serve|to finish|divided for|garnish|croutons|tortilla)\b",
    re.I,
)
DIR_DUP = re.compile(
    r"olive oil\s*,\s*olive oil|olive oil\s+and\s+olive oil|olive oil\s+/\s*olive oil",
    re.I,
)
OR_OIL = re.compile(r"\bolive\s+OR\s+olive\s+oil\b", re.I)
LEADING_AMOUNT = re.compile(
    r"^[\d\s/.\-–—¼½¾⅓⅔⅛⅜⅝⅞]+"
    r"(?:\s*(?:x\s*)?\d+\s*/\s*\d+)?"
    r"(?:\s*(?:tsp|tbsp|teaspoons?|tablespoons?|cups?|ml|g|grams?|kg|l|litres?|liters?))?\b\.?\s*",
    re.I,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def strip_tags(line: str) -> str:
    return SAT_TAG.sub("", line).strip()


def oil_match_key(line: str) -> str:
    s = strip_tags(line).lower()
    s = re.sub(r"\(lf;\s*replaces[^)]*\)", "", s)
    s = re.sub(r"\(i use extra virgin\)", "", s)
    s = re.sub(r"\(or more olive oil\)", "", s)
    s = re.sub(r"\(or more\)", "", s)
    s = re.sub(r"\([^)]*\bml\b[^)]*\)", "", s)
    s = PURPOSE.sub("", s)
    s = re.sub(r"\*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    return s


def is_oil_line(line: str) -> bool:
    return bool(OIL.search(line))


def is_bare_unit_line(line: str) -> bool:
    return bool(BARE_UNIT.match(strip_tags(line)))


def is_broken_amt_oil(line: str) -> bool:
    s = strip_tags(line)
    if not OIL.search(s):
        return False
    if HAS_UNIT.search(s):
        return False
    return bool(BROKEN_AMT_OIL.match(s))


def bare_food(line: str) -> str | None:
    s = strip_tags(line)
    m = BARE_UNIT.match(s)
    if not m:
        return None
    rest = s[m.end() :].strip(" ,")
    rest = SAT_TAG.sub("", rest).strip().lower()
    rest = re.sub(r"\s+", " ", rest)
    return rest or None


def quantified_food(line: str) -> str | None:
    s = strip_tags(line)
    if not LEADING_AMOUNT.match(s):
        return None
    rest = LEADING_AMOUNT.sub("", s, count=1).strip(" ,").lower()
    rest = SAT_TAG.sub("", rest).strip()
    rest = re.sub(r"\s+", " ", rest)
    return rest or None


def fix_ingredients(text: str) -> str | None:
    if not text:
        return None
    original = text
    lines = [OR_OIL.sub("olive oil", ln) for ln in text.splitlines()]
    drop: set[int] = set()

    oil_idxs = [i for i, ln in enumerate(lines) if is_oil_line(ln)]
    quantified_oil = [
        i
        for i in oil_idxs
        if not is_bare_unit_line(lines[i]) and not is_broken_amt_oil(lines[i])
    ]

    for i in oil_idxs:
        if is_bare_unit_line(lines[i]) and quantified_oil:
            drop.add(i)
        elif is_broken_amt_oil(lines[i]) and quantified_oil:
            drop.add(i)

    seen: dict[str, int] = {}
    for i in oil_idxs:
        if i in drop:
            continue
        key = oil_match_key(lines[i])
        if not key:
            continue
        if key in seen:
            prev = seen[key]
            # Prefer line with a purpose clause
            if PURPOSE.search(lines[i]) and not PURPOSE.search(lines[prev]):
                drop.add(prev)
                seen[key] = i
            else:
                drop.add(i)
        else:
            seen[key] = i

    # Bare-unit echoes: "tbsp mustard seeds" vs "1/2 tsp mustard seeds"
    for i, ln in enumerate(lines):
        if i in drop or not is_bare_unit_line(ln):
            continue
        food = bare_food(ln)
        if not food:
            continue
        for j, other in enumerate(lines):
            if j == i or j in drop or is_bare_unit_line(other):
                continue
            qf = quantified_food(other)
            if not qf:
                continue
            # exact food match, or quantified starts with same food words
            if qf == food or qf.startswith(food + " ") or qf.startswith(food + ","):
                drop.add(i)
                break
            # olive oil bare vs any olive oil quantified already handled
            if food == "olive oil" and is_oil_line(other) and not is_bare_unit_line(other):
                drop.add(i)
                break

    # Unscaled name echoes without unit/amount — only near-exact Monash pairs
    # e.g. "Kent pumpkin, peeled..." vs "167 g Kent pumpkin, peeled..."
    for i, ln in enumerate(lines):
        if i in drop:
            continue
        s = strip_tags(ln)
        if LEADING_AMOUNT.match(s) or is_bare_unit_line(s):
            continue
        if not s or s.lower().startswith(("optional", "for the", "salt", "pepper")):
            continue
        # must look like a food description (contains comma or peeled/cut/sliced)
        if not re.search(r",|peeled|cut into|sliced", s, re.I):
            continue
        key = re.sub(r"\s+", " ", s.lower())
        key_cmp = key.replace("1cm", "1 cm").replace("2cm", "2 cm")
        for j, other in enumerate(lines):
            if j == i or j in drop:
                continue
            qf = quantified_food(other)
            if not qf:
                continue
            qf_cmp = qf.replace("1cm", "1 cm").replace("2cm", "2 cm")
            if qf_cmp == key_cmp or qf_cmp.startswith(key_cmp[:40]):
                drop.add(i)
                break

    new_lines = [ln for i, ln in enumerate(lines) if i not in drop]
    new = "\n".join(new_lines)
    if original.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    return None if new == original else new


def fix_directions(text: str) -> str | None:
    if not text:
        return None
    new = DIR_DUP.sub("olive oil", text)
    new = OR_OIL.sub("olive oil", new)
    return None if new == text else new


def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    changes: list[str] = []
    out = dict(rec)
    for field, fn in (("ingredients", fix_ingredients), ("directions", fix_directions)):
        old = rec.get(field) or ""
        new = fn(old)
        if new is None:
            continue
        out[field] = new
        if field == "ingredients":
            removed = [ln for ln in old.splitlines() if ln not in new.splitlines()]
            for ln in removed[:8]:
                changes.append(f"- {ln.strip()[:110]}")
            if not removed:
                changes.append("ingredients: rewritten")
        else:
            changes.append("directions: collapsed duplicate olive oil")
    if not changes:
        return None, []
    return out, changes


def update_local(uid: str, rec: dict) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        """
        UPDATE recipes
        SET ingredients=?, directions=?, status='modified', is_synced=0
        WHERE uid=?
        """,
        (rec.get("ingredients") or "", rec.get("directions") or "", uid),
    )
    con.commit()
    con.close()


def local_candidates() -> list[tuple[str, str]]:
    con = sqlite3.connect(LOCAL_DB)
    out = []
    for uid, name, ing, dire in con.execute(
        """
        select uid, name, ingredients, directions from recipes
        where coalesce(in_trash,0)=0
        """
    ):
        fixed, _ = fix_recipe(
            {
                "name": name,
                "ingredients": ing or "",
                "directions": dire or "",
            }
        )
        if fixed:
            out.append((uid, name or uid))
    con.close()
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sample = """167 g Kent pumpkin, peeled and cut into 1 cm pieces
105 g carrots, peeled and cut into 1 cm pieces
Kent pumpkin, peeled and cut into 1cm pieces
Carrots, peeled and cut into 1cm pieces
1/2 tsp mustard seeds
1/3 tsp ground coriander
1/6 tsp ground cumin
1/6 tsp ground turmeric
1/8 tsp ground cardamom
1/8 tsp chilli powder (optional)
tbsp mustard seeds
1 tsp olive oil ♥ Sat fat 0.6g (3%)
1 tsp olive oil ♥ Sat fat 0.6g (3%)
tbsp olive oil
0.17 l Low FODMAP stock (chicken or vegetable)
83 ml water
"""
    safe_print("DEMO:")
    safe_print(fix_ingredients(sample))

    cands = local_candidates()
    safe_print(f"\nCandidates: {len(cands)}")
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    for uid, name in cands[:40]:
        r = con.execute(
            "select ingredients, directions from recipes where uid=?", (uid,)
        ).fetchone()
        fixed, changes = fix_recipe(
            {
                "name": name,
                "ingredients": r["ingredients"] or "",
                "directions": r["directions"] or "",
            }
        )
        safe_print(f"\n{name}")
        for c in changes[:8]:
            safe_print(f"  {c}")
    con.close()

    if not args.apply:
        safe_print("\nDry-run only. Pass --apply to write.")
        return

    user, pw = paprika_credentials()
    lim = RateLimiter(0.28)
    saved = failed = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        st, body = await api_json(
            s,
            lim,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for i, (uid, name) in enumerate(cands, 1):
            st, body = await api_json(
                s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                failed += 1
                continue
            fixed, changes = fix_recipe(rec)
            if not fixed:
                continue
            safe_print(f"FIX [{i}/{len(cands)}] {rec.get('name')}")
            for c in changes[:6]:
                safe_print(f"  {c}")
            fixed["hash"] = calc_hash(fixed)
            if await save_recipe(s, lim, headers, fixed):
                saved += 1
                update_local(uid, fixed)
            else:
                failed += 1
                safe_print("  FAIL")
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            safe_print("notify", r.status)
    safe_print(f"Done. saved={saved} failed={failed} candidates={len(cands)}")


if __name__ == "__main__":
    asyncio.run(main())
