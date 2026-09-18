"""Deconstruct mis-parsed Field Doctor stock/sauce ingredient blobs.

FD labels list chicken stock as e.g. Chicken Stock (Chicken Extract, Chicken Fat, Salt)
then separate spices. A bracket typo (Salt]) caused spices to nest inside one Paprika line.
"""
from __future__ import annotations

import asyncio
import gzip
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
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

# Leading measure on a blob line (discarded — whole blend tsp is meaningless)
QTY_RE = re.compile(
    r"^(?P<qty>(?:pinch of|sprinkle of|\d+\s+\d/\d\s+tsp|\d+/\d\s+tsp|"
    r"\d+\s+tsp|\d+\s+\d/\d\s+tbsp|\d+/\d\s+tbsp|\d+\s+tbsp))\s+",
    re.I,
)

# Known composites → cookable lines for ~8 portions
COMPOSITE_MAP = [
    (
        re.compile(r"(?i)^chicken stock\b"),
        "500 ml low FODMAP chicken stock",
    ),
    (
        re.compile(r"(?i)^fish sauce\b"),
        "2 tbsp fish sauce",
    ),
    (
        re.compile(r"(?i)^tamari\b"),
        "2 tbsp tamari",
    ),
    (
        re.compile(r"(?i)^sundried tomatoes\b"),
        "20 g sundried tomatoes",
    ),
]

# Single tokens after cleaning → cookable line (None = skip / already covered)
TOKEN_MAP: dict[str, str | None] = {
    "tapioca starch": "2 tsp tapioca starch",
    "yeast extract": "1 tsp yeast extract",
    "herb mix": "2 tsp mixed herbs",
    "mixed herbs": "2 tsp mixed herbs",
    "lemon juice": "1 tbsp lemon juice",
    "sea salt": "sea salt",
    "black pepper": "black pepper",
    "garam masala": "2 tsp garam masala",
    "sweet noble paprika": "2 tsp sweet paprika",
    "sweet paprika": "2 tsp sweet paprika",
    "smoked paprika": "1 tsp smoked paprika",
    "lemon zest": "zest of 1 lemon",
    "chive rings": "20 g chives, sliced",
    "chive": "20 g chives, sliced",
    "chives": "20 g chives, sliced",
    "parsley flat": "handful flat-leaf parsley",
    "forest mix mushrooms": "50 g mixed mushrooms",
    "brown sugar": "1 tsp brown sugar",
    "turmeric blend": "1 tsp turmeric",
    "turmeric": "1 tsp turmeric",
    "cumin seeds (whole)": "1 tsp cumin seeds",
    "cumin seeds": "1 tsp cumin seeds",
    "fennel ground": "1 tsp ground fennel",
    "coriander ground": "1 tsp ground coriander",
    "coriander": "handful fresh coriander",
    "chilli powder": "1/2 tsp chilli powder",
    "red chilli": "1 red chilli",
    "chipotle chilli": "1/2 tsp chipotle chilli",
    "lime leaf": "4 lime leaves",
    "galangal": "1 tbsp galangal",
    "lemongrass": "2 tbsp lemongrass",
    "basil": "handful Thai basil",
    "nutmeg ground": "pinch of nutmeg",
    "saffron": "pinch of saffron",
    "pumpkin seed": "sprinkle of pumpkin seeds",
    "sunflower seed": "sprinkle of sunflower seeds",
    "persian spice mix": "2 tsp Persian spice mix",
}


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def split_top_level(raw: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth_paren = depth_brack = 0
    for ch in raw:
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack = max(0, depth_brack - 1)
        if ch == "," and depth_paren == 0 and depth_brack == 0:
            part = "".join(buf).strip(" ,.")
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip(" ,.")
    if tail:
        parts.append(tail)
    return parts


def fix_mismatched_closers(s: str) -> str:
    """Repair FD typo Salt]/Sugar] that broke stock/sauce paren matching.

    Do not touch Tamari [… sea salt] allergen brackets.
    """
    # Exact chicken-stock shape used on FD labels
    s = re.sub(
        r"(?i)Chicken Stock\s*\(\s*Chicken Extract,\s*Chicken Fat,\s*Salt\]",
        "Chicken Stock (Chicken Extract, Chicken Fat, Salt)",
        s,
    )

    # Fish Sauce (…) may contain nested parens; close at erroneous Sugar]
    def _close_fish(m: re.Match[str]) -> str:
        body = m.group(0)
        return re.sub(r"(?i),\s*Sugar\]", ", Sugar)", body, count=1)

    s = re.sub(
        r"(?i)Fish Sauce\s*\((?:[^()]|\([^()]*\))*?,\s*Sugar\]",
        _close_fish,
        s,
    )
    # Fallback if nested deeper than the regex above
    if re.search(r"(?i)Fish Sauce\s*\(.*Sugar\]", s):
        i = s.lower().find("fish sauce")
        j = s.lower().find("sugar]", i)
        if j != -1:
            s = s[:j] + "Sugar)" + s[j + 6 :]
    return s

def strip_sub_recipe(name: str) -> str:
    """Drop nested bracket contents for matching: Tamari [water…] → Tamari."""
    name = re.sub(r"\[[^\]]*\]", "", name)
    name = re.sub(r"\([^)]*\)", "", name)
    return re.sub(r"\s+", " ", name).strip(" ,.")


def normalize_key(name: str) -> str:
    return strip_sub_recipe(name).lower().strip()


def token_to_line(token: str) -> str | None:
    raw = token.strip()
    key = normalize_key(raw)
    for cre, line in COMPOSITE_MAP:
        if cre.search(raw) or cre.search(key):
            return line
    if key in TOKEN_MAP:
        return TOKEN_MAP[key]
    # fuzzy: startswith known keys
    for k, line in TOKEN_MAP.items():
        if key == k or key.startswith(k + " "):
            return line
    # fallback: keep cleaned name with small cookable amount for spice-like words
    if any(
        w in key
        for w in (
            "paprika",
            "cumin",
            "fennel",
            "chilli",
            "chili",
            "pepper",
            "herb",
            "spice",
            "turmeric",
            "coriander",
            "masala",
            "saffron",
            "galangal",
            "lemongrass",
            "basil",
            "parsley",
            "chive",
            "zest",
            "sugar",
            "starch",
            "salt",
            "mushroom",
            "seed",
        )
    ):
        nice = strip_sub_recipe(raw)
        if re.search(r"(?i)salt|pepper$", nice):
            return nice.lower()
        if re.search(r"(?i)parsley|coriander|basil|chive", nice):
            return f"handful {nice.lower()}"
        return f"1 tsp {nice.lower()}"
    nice = strip_sub_recipe(raw)
    return nice if nice else None


def is_blob_line(line: str) -> bool:
    """True for mis-nested stock/sauce lines — not plain Tamari [allergens] alone."""
    s = line.strip()
    if not s:
        return False
    # Misparsed: stock/sauce opened with ( and wrongly closed with ]
    if re.search(r"(?i)(chicken stock|fish sauce)\s*\(", s) and (
        "Salt]" in s or "Sugar]" in s
    ):
        return True
    # Stock/sauce line that swallowed trailing spices (3+ commas after stock)
    if re.search(r"(?i)(chicken stock|fish sauce)\s*\(", s) and s.count(",") >= 3:
        # Plain "2 tsp Tamari [water, SOYA beans, sea salt]" has commas but no stock
        return True
    # pinch of Chicken Stock (…], Spice, Spice
    if re.search(r"(?i)^(pinch of|.*?tsp)\s+chicken stock\b", s) and s.count(",") >= 2:
        return True
    return False

def deconstruct_blob(line: str) -> list[str]:
    s = line.strip()
    s = QTY_RE.sub("", s)
    s = fix_mismatched_closers(s)
    parts = split_top_level(s)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        mapped = token_to_line(part)
        if not mapped:
            continue
        k = normalize_key(mapped)
        # dedupe similar (sweet paprika vs paprika)
        if k in seen:
            continue
        seen.add(k)
        out.append(mapped)
    return out


def already_has(ingredients: str, new_line: str) -> bool:
    """True if a similar ingredient already exists outside blobs."""
    key = normalize_key(new_line)
    key_core = re.sub(
        r"^(?:\d[\d\s/]*\s*(?:ml|g|tsp|tbsp|litres?)\s+|pinch of|sprinkle of|"
        r"handful|zest of \d+\s+|juice of \d+\s+|low fodmap\s+)",
        "",
        key,
    ).strip()
    # Require a meaningful core (avoid matching empty / very short)
    if len(key_core) < 4:
        return False
    for ln in ingredients.splitlines():
        if is_blob_line(ln):
            continue
        lk = normalize_key(ln)
        lk_core = re.sub(
            r"^(?:\d[\d\s/]*\s*(?:ml|g|tsp|tbsp|litres?|medium)\s+|pinch of|"
            r"sprinkle of|handful|.*?x\s+400\s+g\s+tins?\s+)",
            "",
            lk,
        ).strip()
        if not lk_core:
            continue
        # Exact-ish core match only (avoid "coriander" killing "ground coriander"
        # when fresh coriander already listed — still OK to skip duplicate fresh)
        if key_core == lk_core or key_core in lk_core.split() and lk_core in key_core:
            return True
        if key_core == lk_core or (
            len(key_core) >= 5 and (key_core == lk_core or lk_core.endswith(key_core))
        ):
            return True
        # "2 tbsp Coriander" vs "handful fresh coriander"
        if "coriander" in key_core and "coriander" in lk_core:
            if "ground" in key_core and "ground" not in lk_core:
                continue
            return True
        if "chive" in key_core and "chive" in lk_core:
            return True
        if "lemongrass" in key_core and "lemongrass" in lk_core:
            return True
        if "brown sugar" in key_core and "brown sugar" in lk_core:
            return True
        if key_core in ("sea salt", "black pepper", "salt") and key_core in lk:
            return True
    return False

def fix_ingredients(ingredients: str) -> tuple[str, int]:
    lines = ingredients.splitlines()
    new_lines: list[str] = []
    blobs_fixed = 0
    pending: list[str] = []
    for ln in lines:
        if is_blob_line(ln):
            blobs_fixed += 1
            pending.extend(deconstruct_blob(ln))
        else:
            new_lines.append(ln)
    # append deconstructed, skipping dupes vs remaining list
    draft = "\n".join(new_lines)
    for item in pending:
        if already_has(draft, item):
            continue
        new_lines.append(item)
        draft = "\n".join(new_lines)
    return "\n".join(new_lines).strip() + "\n", blobs_fixed


def patch_directions(directions: str, name: str) -> str:
    """Ensure a brief bloom-spices cue if missing."""
    d = directions or ""
    if re.search(r"(?i)(fry spices|bloom|warm.*oil with|spice base|toast.*paprika)", d):
        return d
    # Insert after first brown/soften step if spices present in text later
    if not re.search(
        r"(?i)(paprika|garam|turmeric|cumin|persian|chilli|masala|saffron)", d
    ):
        return d
    cue = (
        "\n\nSpice tip: briefly fry ground spices (paprika, masala, turmeric, etc.) "
        "in oil with the aromatics before adding stock or other liquids.\n"
    )
    if "Spice tip:" in d:
        return d
    return d.rstrip() + cue


def risotto_stock_tweak(ingredients: str, name: str) -> str:
    if "risotto" not in name.lower():
        return ingredients
    return ingredients.replace(
        "500 ml low FODMAP chicken stock",
        "1 litre low FODMAP chicken stock",
    )


async def post_recipe(session, headers, recipe: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(recipe),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
        headers=headers,
        data=form,
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


def scan_local() -> list[sqlite3.Row]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, directions, notes
        FROM recipes
        WHERE in_trash = 0
          AND lower(name) LIKE 'field doctor%'
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    hits = []
    for r in rows:
        if any(is_blob_line(ln) for ln in (r["ingredients"] or "").splitlines()):
            hits.append(r)
    return hits


async def main() -> None:
    hits = scan_local()
    safe_print(f"Found {len(hits)} Field Doctor recipes with stock/sauce blobs")
    if not hits:
        return

    plans: list[tuple[sqlite3.Row, str, str]] = []
    for r in hits:
        new_ing, n = fix_ingredients(r["ingredients"] or "")
        new_ing = risotto_stock_tweak(new_ing, r["name"])
        new_dir = patch_directions(r["directions"] or "", r["name"])
        safe_print(f"\n{r['name']} ({n} blob line(s))")
        # show what changed at end
        old_blobs = [
            ln for ln in (r["ingredients"] or "").splitlines() if is_blob_line(ln)
        ]
        for b in old_blobs:
            safe_print(f"  WAS: {b[:100]}...")
        added = [
            ln
            for ln in new_ing.splitlines()
            if ln and ln not in (r["ingredients"] or "").splitlines()
        ]
        for a in added:
            safe_print(f"  NOW: {a}")
        plans.append((r, new_ing, new_dir))

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
    ok_n = fail_n = 0
    con = sqlite3.connect(DB)

    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        for r, new_ing, new_dir in plans:
            uid = r["uid"]
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = body.get("result") or {}
            if not rec.get("uid"):
                safe_print(f"API miss {r['name']}")
                fail_n += 1
                continue
            rec["ingredients"] = new_ing
            rec["directions"] = new_dir
            note = (rec.get("notes") or "").rstrip()
            stamp = (
                "\n\n[Stock blob fix] Deconstructed mis-parsed chicken stock / fish sauce "
                "label line into separate cookable ingredients (spices are not part of the stock)."
            )
            if "[Stock blob fix]" not in note:
                rec["notes"] = (note + stamp).strip() + "\n"
            rec["hash"] = calc_hash(rec)
            await limiter.wait_turn()
            ok = await post_recipe(s, H, rec)
            safe_print(f"{'OK' if ok else 'FAIL'} API {r['name']}")
            if ok:
                ok_n += 1
                con.execute(
                    """
                    UPDATE recipes
                    SET ingredients = ?, directions = ?, notes = ?, status = 'unmodified'
                    WHERE uid = ?
                    """,
                    (
                        rec["ingredients"],
                        rec["directions"],
                        rec.get("notes") or "",
                        uid,
                    ),
                )
            else:
                fail_n += 1

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as resp:
            safe_print(f"notify: {resp.status}")

    con.commit()
    con.close()

    # re-scan
    left = scan_local()
    safe_print(f"\nDone ok={ok_n} fail={fail_n}; remaining blobs={len(left)}")
    for r in left:
        safe_print(f"  still: {r['name']}")


if __name__ == "__main__":
    asyncio.run(main())
