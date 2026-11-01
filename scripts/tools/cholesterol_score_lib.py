"""Blood-lipid proxy scoring for Paprika recipes.

Scores are **per portion**: amounts are divided by the recipe `servings`
field (default 1), same as the FODMAP scorer.

Tilted toward **saturated fat** (primary driver of blood LDL for typical adults).
Title / description icons: ♡ low · ❣ mid · 💔 high
Ingredient low icon: 💚 (outline ♡ does not read well in Paprika; the legacy filled ♥
looked alarming, so it is replaced on re-score).
Display: Sat fat g + % of ~20 g/day kitchen reference.
Strips legacy dietary-cholesterol tags on re-score.
Proxy for meal picking — not medical advice.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from collections.abc import Callable

from fodmap_score_lib import (  # noqa: E402
    MAX_LINK_DEPTH,
    linked_recipe,
    linked_servings,
    parse_line,
    parse_servings,
    strip_inline_fodmap,
    to_grams,
)

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "data" / "cholesterol_seed.json"

CHOL_LOW = "♡"
CHOL_LOW_ING = "💚"  # green heart on ingredient lines only (visibility)
CHOL_LOW_ING_LEGACY = "♥"
CHOL_MID = "❣"
CHOL_HIGH = "💔"
CHOL_MARKERS = (CHOL_LOW, CHOL_LOW_ING, CHOL_LOW_ING_LEGACY, CHOL_MID, CHOL_HIGH, "◐")
CHOL_ICON_CLASS = rf"(?:{CHOL_LOW}|{CHOL_LOW_ING}|{CHOL_LOW_ING_LEGACY}|{CHOL_MID}|{CHOL_HIGH}|◐)"

FODMAP_MARKERS = ("✅", "ℹ️", "⚠️", "❌", "🟢", "🟡", "🟠", "🔴")

# ~20 g/day adult kitchen reference (conservative UK-style sat-fat day budget)
DAILY_VALUE_G = 20.0
MEAL_LOW_MAX = 5.0
MEAL_MID_MAX = 10.0
ING_LOW_MAX = 2.0
ING_MID_MAX = 4.0

# Current + legacy tag bodies
_TAG_BODY = (
    r"(?:Sat\s+fat\s+\d+(?:\.\d+)?g(?:\s*\(\d+%\))?"
    r"|Cholesterol\s+\d+mg(?:\s*\(\d+%\))?)"
)
_DESC_CHOL_RE = re.compile(
    rf"\n*{CHOL_ICON_CLASS}\s+{_TAG_BODY}\s*$",
    re.M,
)
_INLINE_CHOL_RE = re.compile(
    rf"\s+{CHOL_ICON_CLASS}\s+{_TAG_BODY}\s*$"
)
_TITLE_MARKER_RE = re.compile(
    r"^(?P<m>(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴|♡|❣|◐|💔|♥|💚))\s*"
)


def pct_of_daily(grams: float) -> int:
    return int(round(100.0 * grams / DAILY_VALUE_G))


def format_sat_tag(icon: str, grams: float) -> str:
    g = round(grams, 1)
    if abs(g - round(g)) < 0.05:
        g_s = str(int(round(g)))
    else:
        g_s = f"{g:.1f}"
    return f"{icon} Sat fat {g_s}g ({pct_of_daily(grams)}%)"


def chol_emoji_meal(grams: float) -> str:
    if grams <= MEAL_LOW_MAX:
        return CHOL_LOW
    if grams <= MEAL_MID_MAX:
        return CHOL_MID
    return CHOL_HIGH


def chol_emoji_ingredient(grams: float) -> str:
    if grams <= ING_LOW_MAX:
        return CHOL_LOW_ING
    if grams <= ING_MID_MAX:
        return CHOL_MID
    return CHOL_HIGH


def strip_inline_chol(line: str) -> str:
    return _INLINE_CHOL_RE.sub("", line).rstrip()


def strip_desc_chol(desc: str) -> str:
    desc = (desc or "").rstrip()
    desc = _DESC_CHOL_RE.sub("", desc)
    desc = re.sub(
        rf"\n*{CHOL_ICON_CLASS}\s+(?:Sat\s+fat|Cholesterol)[^\n]*\s*$",
        "",
        desc,
    )
    desc = re.sub(r"\n*(?:Sat\s+fat\s+\d+(?:\.\d+)?g|Cholesterol\s+\d+mg)[^\n]*\s*$", "", desc)
    return desc.rstrip()


def split_title_markers(name: str) -> tuple[str | None, str | None, str]:
    fodmap = None
    chol = None
    base = (name or "").strip()
    while True:
        m = _TITLE_MARKER_RE.match(base)
        if not m:
            break
        tok = m.group("m")
        if tok in FODMAP_MARKERS:
            if tok in ("🟢", "🟡", "🟠", "🔴", "⚠️"):
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
    old_f, _old_c, base = split_title_markers(name)
    parts = []
    if old_f in ("✅", "ℹ️", "❌", "⚠️"):
        parts.append(old_f)
    elif old_f in ("🟢", "🟡", "🟠", "🔴"):
        parts.append({"🟢": "✅", "🟡": "ℹ️", "🟠": "ℹ️", "🔴": "❌"}[old_f])
    parts.append(emoji)
    parts.append(base)
    return " ".join(parts)


@lru_cache(maxsize=1)
def load_foods() -> list[tuple[str, dict]]:
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    keyed: list[tuple[str, dict]] = []
    for f in data["foods"]:
        for k in f.get("match_keys") or []:
            keyed.append((k.lower(), f))
    keyed.sort(key=lambda x: len(x[0]), reverse=True)
    return keyed


def _clean_rest(rest: str) -> str:
    n = strip_inline_fodmap(strip_inline_chol(rest)).lower()
    n = re.sub(r"\[recipe:([^\]]+)\]", r"\1", n)
    n = re.sub(r"\[[^\]]*\]", " ", n)
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def match_sat_food(rest: str) -> dict | None:
    n = _clean_rest(rest)
    if not n:
        return None
    if re.search(r"\b(stock|broth)\b", n):
        return None
    # The first food named wins ("sour cream or coconut cream" is sour cream), and of
    # keys starting at the same place the longest ("low fat cheese" over "cheese").
    best: tuple[int, int, dict] | None = None
    for key, food in load_foods():
        m = re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", n)
        if not m:
            continue
        if re.search(r"cream$", key) and re.match(r"\s*cheese", n[m.end():]):
            continue  # "lactose-free cream cheese" is cream cheese, not cream
        if key in {"egg", "eggs"} and re.search(r"eggplant|aubergine", n):
            continue
        if key == "cream" and re.search(
            r"cream\s+of|ice\s+cream|non[-\s]?dairy|soy\s+cream", n
        ):
            continue
        if key == "cheese" and re.search(r"dairy[-\s]?free|vegan\s+cheese", n):
            continue
        if key == "milk" and re.search(
            r"\b(oat|almond|soy|soya|rice|coconut|plant|non[-\s]?dairy)\b", n
        ):
            continue
        if key == "fish" and re.search(r"fish\s+sauce", n):
            continue
        if key == "chocolate" and re.search(r"chocolate\s+chip", n):
            continue
        # how far back the match reaches: "lactose-free sour cream" starts before "cream"
        cand = (m.start(), -len(key), food)
        if best is None or cand[:2] < best[:2]:
            best = cand
    return best[2] if best else None


def line_sat_fat_g(line: str) -> tuple[str, float | None, dict | None]:
    clean_out = strip_inline_chol(line).rstrip()  # keeps FODMAP tags
    if "see that recipe" in line.lower() or "[recipe:" in line:  # a linked recipe is scored on its own
        return clean_out, None, None
    p = parse_line(strip_inline_fodmap(strip_inline_chol(line)))
    if not p:
        return clean_out, None, None
    food = match_sat_food(p.rest)
    if not food:
        return clean_out, None, None
    grams_sat = None
    if p.unit == "count" and food.get("sat_fat_g_per_count") is not None:
        grams_sat = p.value * float(food["sat_fat_g_per_count"])
    else:
        mass = to_grams(p)
        if mass is None and p.unit == "count" and food.get("count_g"):
            mass = p.value * float(food["count_g"])
        if mass is not None and food.get("sat_fat_g_per_100g") is not None:
            grams_sat = mass * float(food["sat_fat_g_per_100g"]) / 100.0
    return clean_out, grams_sat, food


def annotate_ingredients(
    ingredients: str,
    servings: float | str | int | None = 1.0,
    linked: Callable[[str], dict | None] | None = None,
    _depth: int = 0,
) -> tuple[str, float, list[dict]]:
    """`linked` resolves '[recipe:Name]' lines: the linked recipe's saturated fat per
    serving is added for the servings the line uses."""
    servings_n = parse_servings(servings)
    total = 0.0
    drivers: list[dict] = []
    out: list[str] = []
    for raw in (ingredients or "").splitlines():
        if not raw.strip():
            out.append(raw)
            continue
        rec = linked_recipe(raw, linked) if _depth < MAX_LINK_DEPTH else None
        if rec:
            clean = strip_inline_chol(raw).rstrip()
            _, sub, _ = annotate_ingredients(rec.get("ingredients") or "", rec.get("servings"), linked, _depth + 1)
            portion_g = sub * linked_servings(clean, rec) / servings_n
            if round(portion_g, 1) <= 0:
                out.append(clean)
                continue
            total += portion_g
            icon = chol_emoji_ingredient(portion_g)
            drivers.append({"food": "recipe", "name": rec.get("name"), "g": round(portion_g, 2), "icon": icon})
            out.append(f"{clean}  {format_sat_tag(icon, portion_g)}")
            continue
        clean, g, food = line_sat_fat_g(raw)
        if g is None or g <= 0 or food is None:
            out.append(clean)
            continue
        portion_g = g / servings_n
        if round(portion_g, 1) <= 0:
            out.append(clean)
            continue
        total += portion_g
        icon = chol_emoji_ingredient(portion_g)
        drivers.append(
            {
                "food": food["id"],
                "name": food["name"],
                "g": round(portion_g, 2),
                "icon": icon,
            }
        )
        out.append(f"{clean}  {format_sat_tag(icon, portion_g)}")
    text = "\n".join(out)
    if ingredients.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, total, drivers


def transform_recipe(
    name: str,
    ingredients: str,
    description: str,
    notes: str,
    servings: float | str | int | None = 1.0,
    linked: Callable[[str], dict | None] | None = None,
) -> dict:
    new_ings, total_g, drivers = annotate_ingredients(ingredients, servings=servings, linked=linked)
    meal_em = chol_emoji_meal(total_g)
    new_name = set_title_chol_emoji(name, meal_em)
    desc_base = strip_desc_chol(description)
    summary = format_sat_tag(meal_em, total_g)
    if desc_base:
        new_desc = (desc_base.rstrip() + "\n\n" + summary).strip() + "\n"
    else:
        new_desc = summary + "\n"
    return {
        "name": new_name,
        "ingredients": new_ings,
        "description": new_desc,
        "notes": notes or "",
        "total_g": round(total_g, 2),
        "total_mg": round(total_g, 2),  # legacy key for library script prints
        "pct_daily": pct_of_daily(total_g),
        "meal_emoji": meal_em,
        "drivers": drivers,
    }
