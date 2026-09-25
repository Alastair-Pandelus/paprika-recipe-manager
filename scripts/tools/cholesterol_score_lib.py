"""Dietary-cholesterol proxy scoring for Paprika recipes (1-serve assumed).

Title icons (after FODMAP marker): ♡ low · ❣ mid · 💔 high
Quantified as mg dietary cholesterol from data/cholesterol_seed.json.
Proxy for meal picking — not medical advice / blood-lipid guidance.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from fodmap_score_lib import (  # noqa: E402
    parse_line,
    strip_inline_fodmap,
    to_grams,
)

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "data" / "cholesterol_seed.json"

CHOL_LOW = "♡"
CHOL_MID = "❣"
CHOL_HIGH = "💔"
# Include legacy ◐ / ♥ so re-score replaces previous mid/high markers
CHOL_MARKERS = (CHOL_LOW, CHOL_MID, CHOL_HIGH, "◐", "♥")
CHOL_ICON_CLASS = rf"(?:{CHOL_LOW}|{CHOL_MID}|{CHOL_HIGH}|◐|♥)"

FODMAP_MARKERS = ("✅", "ℹ️", "⚠️", "❌", "🟢", "🟡", "🟠", "🔴")

# Meal total (1 serve) vs ~300 mg/day kitchen reference (classic Daily Value)
DAILY_VALUE_MG = 300
MEAL_LOW_MAX = 100
MEAL_MID_MAX = 200
# Per-ingredient contribution
ING_LOW_MAX = 50
ING_MID_MAX = 100

# Tag shape: ♡ Cholesterol 133mg (44%)  — mg retained; % of DAILY_VALUE_MG
_CHOL_TAG_BODY = r"Cholesterol\s+\d+mg(?:\s*\(\d+%\))?"
_DESC_CHOL_RE = re.compile(
    rf"\n*{CHOL_ICON_CLASS}\s+{_CHOL_TAG_BODY}\s*$",
    re.M,
)
_INLINE_CHOL_RE = re.compile(
    rf"\s+{CHOL_ICON_CLASS}\s+{_CHOL_TAG_BODY}\s*$"
)
_TITLE_MARKER_RE = re.compile(
    r"^(?P<m>(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴|♡|❣|◐|💔|♥))\s*"
)


def pct_of_daily(mg: float) -> int:
    return int(round(100.0 * mg / DAILY_VALUE_MG))


def format_chol_tag(icon: str, mg: float) -> str:
    mg_i = int(round(mg))
    return f"{icon} Cholesterol {mg_i}mg ({pct_of_daily(mg)}%)"


def chol_emoji_meal(mg: float) -> str:
    if mg <= MEAL_LOW_MAX:
        return CHOL_LOW
    if mg <= MEAL_MID_MAX:
        return CHOL_MID
    return CHOL_HIGH


def chol_emoji_ingredient(mg: float) -> str:
    if mg <= ING_LOW_MAX:
        return CHOL_LOW
    if mg <= ING_MID_MAX:
        return CHOL_MID
    return CHOL_HIGH


def strip_inline_chol(line: str) -> str:
    return _INLINE_CHOL_RE.sub("", line).rstrip()


def strip_desc_chol(desc: str) -> str:
    desc = (desc or "").rstrip()
    desc = _DESC_CHOL_RE.sub("", desc)
    # Legacy: icon + mg without %, or bare "Cholesterol Nmg"
    desc = re.sub(
        rf"\n*{CHOL_ICON_CLASS}\s+Cholesterol\s+\d+mg\s*$",
        "",
        desc,
    )
    desc = re.sub(r"\n*Cholesterol\s+\d+mg(?:\s*\(\d+%\))?\s*$", "", desc)
    return desc.rstrip()


def split_title_markers(name: str) -> tuple[str | None, str | None, str]:
    """Return (fodmap_marker, chol_marker, bare_name)."""
    fodmap = None
    chol = None
    base = (name or "").strip()
    while True:
        m = _TITLE_MARKER_RE.match(base)
        if not m:
            break
        tok = m.group("m")
        if tok in FODMAP_MARKERS:
            fodmap = tok if tok in ("✅", "ℹ️", "❌", "⚠️") else fodmap or tok
            # Collapse coloured legacy to nothing special — keep last FODMAP-ish
            if tok in ("🟢", "🟡", "🟠", "🔴", "⚠️"):
                # leave fodmap as prior ✅/ℹ️/❌ if any; else map roughly
                if fodmap in (None, "🟢", "🟡", "🟠", "🔴", "⚠️"):
                    fodmap = {"🟢": "✅", "🟡": "ℹ️", "🟠": "ℹ️", "🔴": "❌", "⚠️": "ℹ️"}.get(
                        tok, fodmap
                    )
            else:
                fodmap = tok
        elif tok in CHOL_MARKERS:
            chol = tok
        base = base[m.end() :]
    return fodmap, chol, base.strip()


def set_title_chol_emoji(name: str, emoji: str) -> str:
    """Set cholesterol marker; preserve leading FODMAP marker if present."""
    old_f, _old_c, base = split_title_markers(name)
    parts = []
    if old_f in ("✅", "ℹ️", "❌", "⚠️"):
        parts.append(old_f)
    elif old_f in ("🟢", "🟡", "🟠", "🔴"):
        parts.append(
            {"🟢": "✅", "🟡": "ℹ️", "🟠": "ℹ️", "🔴": "❌"}[old_f]
        )
    parts.append(emoji)
    parts.append(base)
    return " ".join(parts)

@lru_cache(maxsize=1)
def load_foods() -> list[dict]:
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    foods = data["foods"]
    # longest match_keys first across all foods
    keyed: list[tuple[str, dict]] = []
    for f in foods:
        for k in f.get("match_keys") or []:
            keyed.append((k.lower(), f))
    keyed.sort(key=lambda x: len(x[0]), reverse=True)
    return keyed  # type: ignore[return-value]


def _clean_rest(rest: str) -> str:
    n = strip_inline_fodmap(strip_inline_chol(rest)).lower()
    n = re.sub(r"\[[^\]]*\]", " ", n)
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def match_chol_food(rest: str) -> dict | None:
    n = _clean_rest(rest)
    if not n:
        return None
    # Stocks/broths: skip (chicken stock ≠ chicken meat)
    if re.search(r"\b(stock|broth)\b", n):
        return None
    for key, food in load_foods():
        # word-aware: require key as substring with non-letter boundaries
        if not re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", n):
            continue
        if key in {"egg", "eggs"} and re.search(r"eggplant|aubergine", n):
            continue
        if key == "cream" and re.search(
            r"cream\s+of|ice\s+cream|non[-\s]?dairy|coconut\s+cream|soy\s+cream", n
        ):
            continue
        if key == "cheese" and re.search(r"dairy[-\s]?free|vegan\s+cheese", n):
            continue
        if key == "fish" and re.search(r"fish\s+sauce", n):
            continue
        return food
    return None

def line_cholesterol_mg(line: str) -> tuple[str, float | None, dict | None]:
    """Return (clean_line, mg_or_None, food)."""
    clean = strip_inline_chol(strip_inline_fodmap(line)).rstrip()
    # keep FODMAP tags on output — strip only for parse, then re-attach from original
    # Better: strip chol only for clean base, preserve fodmap tags from original line
    base_for_parse = strip_inline_chol(line)
    clean_out = strip_inline_chol(line).rstrip()  # keeps FODMAP tags
    p = parse_line(strip_inline_fodmap(strip_inline_chol(line)))
    if not p:
        return clean_out, None, None
    food = match_chol_food(p.rest)
    if not food:
        return clean_out, None, None
    mg = None
    if p.unit == "count" and food.get("mg_per_count") is not None:
        mg = p.value * float(food["mg_per_count"])
    else:
        grams = to_grams(p)
        if grams is None and p.unit == "count" and food.get("count_g"):
            grams = p.value * float(food["count_g"])
        if grams is not None and food.get("mg_per_100g") is not None:
            mg = grams * float(food["mg_per_100g"]) / 100.0
    return clean_out, mg, food


def annotate_ingredients(ingredients: str) -> tuple[str, float, list[dict]]:
    total = 0.0
    drivers: list[dict] = []
    out: list[str] = []
    for raw in (ingredients or "").splitlines():
        if not raw.strip():
            out.append(raw)
            continue
        clean, mg, food = line_cholesterol_mg(raw)
        if mg is None or mg <= 0 or food is None:
            out.append(clean)
            continue
        mg_i = int(round(mg))
        if mg_i <= 0:
            out.append(clean)
            continue
        total += mg
        icon = chol_emoji_ingredient(mg)
        drivers.append(
            {"food": food["id"], "name": food["name"], "mg": mg_i, "icon": icon}
        )
        out.append(f"{clean}  {format_chol_tag(icon, mg)}")
    text = "\n".join(out)
    if ingredients.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, total, drivers


def transform_recipe(
    name: str, ingredients: str, description: str, notes: str
) -> dict:
    new_ings, total_mg, drivers = annotate_ingredients(ingredients)
    meal_em = chol_emoji_meal(total_mg)
    new_name = set_title_chol_emoji(name, meal_em)
    desc_base = strip_desc_chol(description)
    total_i = int(round(total_mg))
    summary = format_chol_tag(meal_em, total_mg)
    if desc_base:
        new_desc = (desc_base.rstrip() + "\n\n" + summary).strip() + "\n"
    else:
        new_desc = summary + "\n"
    return {
        "name": new_name,
        "ingredients": new_ings,
        "description": new_desc,
        "notes": notes or "",
        "total_mg": total_i,
        "pct_daily": pct_of_daily(total_mg),
        "meal_emoji": meal_em,
        "drivers": drivers,
    }
