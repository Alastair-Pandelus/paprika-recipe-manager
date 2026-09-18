"""Scale all Paprika recipes to 1 serving from source_url when possible.

Strategy per recipe (servings > 1):
  1. Fetch source_url HTML; parse JSON-LD Recipe (ingredients + yield).
  2. Scale source ingredient quantities to 1 serve (÷ yield).
  3. Remap onto existing Paprika ingredient lines, converting into the
     unit already used on that line when possible (g/ml/tsp/tbsp/cup/count).
  4. Fallback if scrape/parse fails: divide quantities on existing lines by
     the recipe's current servings field.

Field Doctor (fielddoctor.co.uk) is skipped by default — already rebuilt via
fd_single_serve_scrape.py.

  python scripts/tools/library_single_serve.py --dry-run
  python scripts/tools/library_single_serve.py --apply
  python scripts/tools/library_single_serve.py --limit 20 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OUT_DIR = Path(__file__).resolve().parent / ".library_single_serve_dryrun"
LOG_PATH = Path(__file__).resolve().parent / ".library_single_serve.log"

SKIP_HOSTS = {"www.fielddoctor.co.uk", "fielddoctor.co.uk"}

# Approximate densities for converting between mass/volume kitchen units (g per unit).
G_PER = {
    "g": 1.0,
    "kg": 1000.0,
    "oz": 28.35,
    "lb": 453.6,
    "ml": 1.0,  # water-like; used for liquid conversion heuristic
    "l": 1000.0,
    "tsp": 5.0,
    "tbsp": 15.0,
    "cup": 240.0,
}

UNIT_ALIASES = {
    "grams": "g",
    "gram": "g",
    "g": "g",
    "kilograms": "kg",
    "kilogram": "kg",
    "kg": "kg",
    "milliliters": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "millilitre": "ml",
    "ml": "ml",
    "liters": "l",
    "litres": "l",
    "liter": "l",
    "litre": "l",
    "l": "l",
    "teaspoons": "tsp",
    "teaspoon": "tsp",
    "tsp": "tsp",
    "tsps": "tsp",
    "t": "tsp",
    "tablespoons": "tbsp",
    "tablespoon": "tbsp",
    "tbsp": "tbsp",
    "tbsps": "tbsp",
    "tbs": "tbsp",
    "T": "tbsp",
    "cups": "cup",
    "cup": "cup",
    "c": "cup",
    "ounces": "oz",
    "ounce": "oz",
    "oz": "oz",
    "pounds": "lb",
    "pound": "lb",
    "lbs": "lb",
    "lb": "lb",
}

VULGAR = {
    "¼": "1/4",
    "½": "1/2",
    "¾": "3/4",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}

FRACTION_CANDIDATES = [
    Fraction(0),
    Fraction(1, 8),
    Fraction(1, 6),
    Fraction(1, 5),
    Fraction(1, 4),
    Fraction(1, 3),
    Fraction(3, 8),
    Fraction(2, 5),
    Fraction(1, 2),
    Fraction(3, 5),
    Fraction(5, 8),
    Fraction(2, 3),
    Fraction(3, 4),
    Fraction(4, 5),
    Fraction(5, 6),
    Fraction(7, 8),
]


@dataclass
class ParsedQty:
    value: float
    unit: str | None  # normalized unit or None for countable
    rest: str  # remainder of line after qty/unit
    raw_prefix: str  # original qty+unit text


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def parse_servings(text) -> float | None:
    if text is None:
        return None
    s = str(text).strip().lower()
    if not s:
        return None
    # "4 servings", "Serves 6", "8", "4-6" → use first number
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    n = float(m.group(1))
    return n if n > 0 else None


def find_recipe(obj):
    if isinstance(obj, dict):
        typ = obj.get("@type")
        if typ == "Recipe" or (isinstance(typ, list) and "Recipe" in typ):
            return obj
        for g in obj.get("@graph") or []:
            found = find_recipe(g)
            if found:
                return found
        for v in obj.values():
            found = find_recipe(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_recipe(item)
            if found:
                return found
    return None


def extract_recipe_json(page_html: str) -> dict | None:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        re.I | re.S,
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        rec = find_recipe(data)
        if rec and (rec.get("recipeIngredient") or rec.get("recipeInstructions")):
            return rec
    return None


def normalize_unit(u: str | None) -> str | None:
    if not u:
        return None
    key = u.strip().lower().rstrip(".")
    return UNIT_ALIASES.get(key) or UNIT_ALIASES.get(u.strip()) or None


def parse_number_token(tok: str) -> float | None:
    tok = tok.strip()
    for k, v in VULGAR.items():
        tok = tok.replace(k, v)
    tok = tok.replace("⁄", "/")
    if not tok:
        return None
    # mixed: 1-1/2 or 1 1/2
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(int(m.group(1)) + Fraction(int(m.group(2)), int(m.group(3))))
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(int(m.group(1)) + Fraction(int(m.group(2)), int(m.group(3))))
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(Fraction(int(m.group(1)), int(m.group(2))))
    try:
        return float(tok)
    except ValueError:
        return None


_LINE_RE = re.compile(
    r"""^\s*
    (?P<qty>
        (?:[¼½¾⅓⅔⅛⅜⅝⅞]|\d+\s+\d+\s*/\s*\d+|\d+\s*-\s*\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)
        (?:\s*(?:to|-|–|—)\s*
            (?:[¼½¾⅓⅔⅛⅜⅝⅞]|\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)
        )?
    )
    \s*
    (?P<unit>
        kilograms?|kilogram|grams?|gram|kg|g|
        millilitres?|milliliters?|millilitre|milliliter|ml|
        litres?|liters?|litre|liter|l|
        tablespoons?|tablespoon|tbsps?|tbsp|tbs|
        teaspoons?|teaspoon|tsps?|tsp|
        cups?|cup|
        ounces?|ounce|oz|
        pounds?|pound|lbs?|lb
    )?
    \b
    \s*
    (?P<rest>.*)$
    """,
    re.I | re.X,
)


def parse_ingredient_line(line: str) -> ParsedQty | None:
    s = (line or "").strip()
    if not s or s.startswith("#") or s.startswith("["):
        return None
    # skip section headers without quantities
    if re.match(r"^(for the|sauce|dressing|marinade|topping|base)\b", s, re.I) and not re.match(
        r"^\d", s
    ):
        return None
    m = _LINE_RE.match(s)
    if not m:
        return None
    qty_raw = m.group("qty").replace(",", ".")
    # ranges → take midpoint
    parts = re.split(r"\s*(?:to|-|–|—)\s*", qty_raw)
    nums = [parse_number_token(p) for p in parts]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None
    value = sum(nums) / len(nums)
    unit = normalize_unit(m.group("unit"))
    rest = (m.group("rest") or "").strip()
    # strip leading "of "
    rest = re.sub(r"^(?:of\s+)", "", rest, flags=re.I)
    prefix = m.group(0)[: len(m.group(0)) - len(m.group("rest") or "")].strip()
    return ParsedQty(value=value, unit=unit, rest=rest, raw_prefix=prefix)


def name_key(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    stop = {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "for",
        "with",
        "fresh",
        "large",
        "small",
        "medium",
        "optional",
        "chopped",
        "diced",
        "sliced",
        "minced",
        "ground",
        "cooked",
        "raw",
    }
    toks = [w for w in t.split() if w and w not in stop]
    return " ".join(toks[:6])


def format_qty(value: float, unit: str | None) -> str:
    if value < 0:
        value = 0
    # Prefer tsp when tbsp would be a tiny awkward amount
    if unit == "tbsp" and 0 < value < 0.5:
        return format_qty(value * 3.0, "tsp")
    # Prefer tbsp when tsp is large
    if unit == "tsp" and value >= 3.5:
        return format_qty(value / 3.0, "tbsp")

    # grams / ml: integer if >= 10, else 1 decimal
    if unit in {"g", "ml", "oz"}:
        if value >= 10:
            return f"{int(round(value))} {unit}"
        if value >= 1:
            return f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        if value <= 0:
            return f"0 {unit}"
        return f"{value:.2f}".rstrip("0").rstrip(".") + f" {unit}"
    if unit == "kg":
        return f"{value:.2f}".rstrip("0").rstrip(".") + " kg"
    if unit == "l":
        return f"{value:.2f}".rstrip("0").rstrip(".") + " l"
    if unit in {"tsp", "tbsp", "cup", "lb"} or unit is None:
        if value <= 0:
            if unit == "tsp":
                return "1/8 tsp"
            if unit == "tbsp":
                return "1/8 tsp"
            if unit == "cup":
                return "1 tbsp"
            return "0" + (f" {unit}" if unit else "")
        whole = int(math.floor(value + 1e-9))
        frac = value - whole
        # Never snap a positive amount down to zero
        candidates = [f for f in FRACTION_CANDIDATES if not (whole == 0 and f == 0 and value > 0)]
        if value > 0 and whole == 0:
            candidates = [f for f in FRACTION_CANDIDATES if f > 0]
        best = min(candidates, key=lambda f: abs(float(f) - frac))
        # If error is large, keep decimals
        if abs(float(best) - frac) > 0.09:
            q = f"{value:.2f}".rstrip("0").rstrip(".")
        elif best == 0:
            q = str(whole) if whole else "1/8"
        elif whole == 0:
            q = f"{best.numerator}/{best.denominator}"
        else:
            q = f"{whole} {best.numerator}/{best.denominator}"
        if unit:
            return f"{q} {unit}"
        return q
    return f"{value:g} {unit}" if unit else f"{value:g}"


def convert_amount(value: float, from_unit: str | None, to_unit: str | None) -> float | None:
    if from_unit == to_unit:
        return value
    if from_unit is None or to_unit is None:
        # countable ↔ countable only
        if from_unit is None and to_unit is None:
            return value
        return None
    if from_unit not in G_PER or to_unit not in G_PER:
        return None
    # mass family vs volume family — only convert within compatible sets loosely
    mass = {"g", "kg", "oz", "lb"}
    vol = {"ml", "l", "tsp", "tbsp", "cup"}
    if (from_unit in mass and to_unit in mass) or (from_unit in vol and to_unit in vol):
        grams = value * G_PER[from_unit]
        return grams / G_PER[to_unit]
    return None


def scale_parsed(p: ParsedQty, factor: float) -> str:
    new_v = p.value * factor
    qty = format_qty(new_v, p.unit)
    return f"{qty} {p.rest}".strip()


def scale_line_text(line: str, factor: float) -> tuple[str, bool]:
    """Scale leading quantity on a line; return (new_line, changed)."""
    if factor == 1.0:
        return line, False
    p = parse_ingredient_line(line)
    if not p:
        return line, False
    return scale_parsed(p, factor), True


def best_match(src_rest: str, candidates: list[tuple[int, ParsedQty, str]]) -> int | None:
    key = name_key(src_rest)
    if not key:
        return None
    best_i = None
    best_score = 0
    sk = set(key.split())
    for idx, pq, _raw in candidates:
        ck = set(name_key(pq.rest).split())
        if not ck:
            continue
        score = len(sk & ck)
        if score > best_score and score >= 1:
            best_score = score
            best_i = idx
    return best_i


def remap_to_existing_units(
    source_lines: list[str],
    existing_text: str,
    factor: float,
) -> tuple[str, str]:
    """Scale source lines and rewrite using existing Paprika units where matched.

    Returns (new_ingredients_text, method_tag).
    """
    existing_lines = [ln for ln in (existing_text or "").splitlines()]
    parsed_existing: list[tuple[int, ParsedQty, str]] = []
    for i, ln in enumerate(existing_lines):
        p = parse_ingredient_line(ln)
        if p:
            parsed_existing.append((i, p, ln))

    used: set[int] = set()
    out_by_index: dict[int, str] = {}
    extras: list[str] = []
    matched = 0

    for src in source_lines:
        sp = parse_ingredient_line(src)
        if not sp:
            # section header or free text — keep as-is if no qty
            if src.strip():
                extras.append(src.strip())
            continue
        scaled_val = sp.value * factor
        cand = [(i, p, raw) for i, p, raw in parsed_existing if i not in used]
        mi = best_match(sp.rest, cand)
        if mi is None:
            extras.append(scale_parsed(sp, factor))
            continue
        # find ParsedQty for mi
        ep = next(p for i, p, _ in parsed_existing if i == mi)
        eraw = next(raw for i, _, raw in parsed_existing if i == mi)
        converted = convert_amount(scaled_val, sp.unit, ep.unit)
        if converted is None:
            # keep existing unit text structure but scale existing qty by factor
            # (source unit incompatible) — prefer scaling existing line
            out_by_index[mi] = scale_parsed(ep, factor)
        else:
            qty = format_qty(converted, ep.unit)
            # preserve everything after the original qty/unit on the paprika line
            rest = ep.rest
            out_by_index[mi] = f"{qty} {rest}".strip()
        used.add(mi)
        matched += 1

    # Unmatched existing lines: scale by same factor (still in recipe)
    for i, p, raw in parsed_existing:
        if i in used:
            continue
        out_by_index[i] = scale_parsed(p, factor)

    # Rebuild in original order; append unmatched source extras at end
    new_lines: list[str] = []
    for i, ln in enumerate(existing_lines):
        if not ln.strip():
            continue
        if i in out_by_index:
            new_lines.append(out_by_index[i])
        else:
            # unparsed existing (headers etc.)
            new_lines.append(ln)

    for ex in extras:
        if ex not in new_lines:
            new_lines.append(ex)

    method = f"source_remap matched={matched}/{len(parsed_existing)}"
    return "\n".join(new_lines).rstrip() + ("\n" if new_lines else ""), method


def scale_existing_only(existing_text: str, factor: float) -> tuple[str, str]:
    lines = []
    scaled_n = 0
    for ln in (existing_text or "").splitlines():
        if not ln.strip():
            continue
        new_ln, changed = scale_line_text(ln, factor)
        if changed:
            scaled_n += 1
        lines.append(new_ln)
    return "\n".join(lines).rstrip() + ("\n" if lines else ""), f"fallback_divide scaled_lines={scaled_n}"


def list_recipes(limit: int = 0, *, include_fd: bool = False) -> list[dict]:
    skip = set() if include_fd else SKIP_HOSTS
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, notes, directions, servings, source_url
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        n = parse_servings(r["servings"])
        if n is None or n <= 1.01:
            continue
        url = (r["source_url"] or "").strip()
        host = urlparse(url).netloc.lower() if url else ""
        if host in skip:
            continue
        out.append(
            {
                "uid": r["uid"],
                "name": r["name"] or "",
                "ingredients": r["ingredients"] or "",
                "notes": r["notes"] or "",
                "directions": r["directions"] or "",
                "servings": r["servings"],
                "servings_n": n,
                "source_url": url,
            }
        )
    if limit and limit > 0:
        out = out[:limit]
    return out


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; PaprikaSingleServe/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    ) as r:
        r.raise_for_status()
        return await r.text()


async def post_recipe(session, headers, recipe: dict, limiter: RateLimiter) -> bool:
    await limiter.wait_turn()
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


def update_local(uid: str, ingredients: str, servings: str) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "UPDATE recipes SET ingredients=?, servings=?, status=? WHERE uid=?",
        (ingredients, servings, "modified", uid),
    )
    con.commit()
    con.close()


def process_recipe_payload(
    local: dict, html: str | None
) -> dict:
    n = local["servings_n"]
    factor_fallback = 1.0 / n
    method = "fallback_divide"
    new_ings = None
    source_yield = None

    if html:
        rec = extract_recipe_json(html)
        if rec:
            ings = rec.get("recipeIngredient") or []
            if isinstance(ings, list) and ings:
                src_lines = [str(x).strip() for x in ings if str(x).strip()]
                y = parse_servings(rec.get("recipeYield"))
                source_yield = y
                factor = 1.0 / y if y and y > 1.01 else factor_fallback
                new_ings, method = remap_to_existing_units(
                    src_lines, local["ingredients"], factor
                )

    if new_ings is None:
        new_ings, method = scale_existing_only(local["ingredients"], factor_fallback)

    return {
        "uid": local["uid"],
        "name": local["name"],
        "source_url": local["source_url"],
        "old_servings": local["servings"],
        "old_servings_n": n,
        "source_yield": source_yield,
        "new_servings": "1",
        "method": method,
        "old_ingredients": local["ingredients"],
        "new_ingredients": new_ings,
        "changed": new_ings.strip() != (local["ingredients"] or "").strip(),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--include-fd", action="store_true", help="Also process Field Doctor URLs")
    args = ap.parse_args()

    apply = bool(args.apply)
    mode = "APPLY" if apply else "DRY-RUN"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recipes = list_recipes(args.limit, include_fd=bool(args.include_fd))
    safe_print(f"Library single-serve | {mode} | candidates={len(recipes)}")

    results = []
    stats = {"source_remap": 0, "fallback_divide": 0, "fetch_fail": 0, "unchanged": 0}
    limiter = RateLimiter(0.3)
    headers = None
    updated = 0

    if apply:
        user, pw = paprika_credentials()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        if apply:
            st, body = await api_json(
                session,
                limiter,
                "POST",
                f"{PAPRIKA_API}/v1/account/login",
                data={"email": user, "password": pw},
            )
            if st != 200:
                raise SystemExit(f"login failed {st}")
            headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for i, local in enumerate(recipes, 1):
            html = None
            url = local["source_url"]
            if url:
                try:
                    html = await fetch_html(session, url)
                except Exception as e:  # noqa: BLE001
                    stats["fetch_fail"] += 1
                    safe_print(f"FETCH_FAIL {local['name'][:48]} | {e}")

            art = process_recipe_payload(local, html)
            if art["method"].startswith("source_remap"):
                stats["source_remap"] += 1
            else:
                stats["fallback_divide"] += 1
            if not art["changed"] and str(local["servings"]).strip() == "1":
                stats["unchanged"] += 1

            out = OUT_DIR / f"{local['uid']}.json"
            out.write_text(json.dumps(art, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            results.append(
                {
                    "uid": art["uid"],
                    "name": art["name"],
                    "method": art["method"],
                    "old_servings": art["old_servings"],
                    "changed": art["changed"],
                }
            )

            if i % 25 == 0 or i == len(recipes):
                safe_print(
                    f"progress {i}/{len(recipes)} source={stats['source_remap']} "
                    f"fallback={stats['fallback_divide']} fetch_fail={stats['fetch_fail']}"
                )

            if apply:
                assert headers is not None
                st, body = await api_json(
                    session,
                    limiter,
                    "GET",
                    f"{PAPRIKA_API}/v2/sync/recipe/{local['uid']}/",
                    headers=headers,
                )
                rec = (body or {}).get("result") or {}
                if not rec.get("uid"):
                    safe_print(f"FAIL missing {local['name']}")
                    continue
                rec["ingredients"] = art["new_ingredients"]
                rec["servings"] = "1"
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(session, headers, rec, limiter)
                if ok:
                    updated += 1
                    update_local(local["uid"], art["new_ingredients"], "1")
                else:
                    safe_print(f"FAIL save {local['name']}")

            await asyncio.sleep(0.15)

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    summary = {
        "mode": mode,
        "candidates": len(recipes),
        "stats": stats,
        "updated": updated if apply else 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recipes": results,
    }
    (OUT_DIR / "_index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    LOG_PATH.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    safe_print(
        f"Done | candidates={len(recipes)} source_remap={stats['source_remap']} "
        f"fallback={stats['fallback_divide']} updated={updated if apply else 0}"
    )


if __name__ == "__main__":
    asyncio.run(main())
