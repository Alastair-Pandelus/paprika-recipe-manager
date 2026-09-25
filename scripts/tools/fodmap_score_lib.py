"""FODMAP proxy scoring helpers for Paprika recipes (1-serve assumed).

Green-serve table lives in data/monash_fodmap.sqlite (upsert via
scripts/tools/upsert_monash_fodmap.py). Official Monash app data is
proprietary — maintain rows by checking the app and upserting.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

TYPE_NAME = {
    "FRU": "Fructose",
    "LAC": "Lactose",
    "FRT": "Fructans",
    "GOS": "Galacto-oligosaccharides",
    "SOR": "Sorbitol",
    "MAN": "Mannitol",
}
TYPE_ORDER = ("FRU", "FRT", "LAC", "GOS", "SOR", "MAN")

BLOCK_START = "FODMAP review (1 serve)"
BLOCK_END = "— end FODMAP review —"

MONASH_DB = Path(__file__).resolve().parents[2] / "data" / "monash_fodmap.sqlite"

# Approximate g per kitchen unit when converting volume → mass for scoring
G_PER_UNIT = {
    "g": 1.0,
    "kg": 1000.0,
    "oz": 28.35,
    "lb": 453.6,
    "ml": 1.0,
    "l": 1000.0,
    "tsp": 5.0,
    "tbsp": 15.0,
    "cup": 240.0,
}

# Density overrides (g per tsp) for common pastes/sauces
DENSITY_TSP: list[tuple[str, float]] = [
    ("tomato puree", 5.5),
    ("tomato paste", 5.5),
    ("tamari", 6.0),
    ("soy sauce", 6.0),
    ("olive oil", 4.5),
    ("vinegar", 5.0),
    ("stock", 5.0),
]

# Whole-item estimates (g)
ITEM_G = {
    "red pepper": 160.0,
    "yellow pepper": 160.0,
    "orange pepper": 160.0,
    "green pepper": 160.0,
    "pepper": 160.0,  # bell, when not black/white pepper
    "carrot": 60.0,
    "leek": 160.0,  # whole; greens-only overridden in to_grams (~75 g / cup)
    "spring onion": 12.0,  # green tops of one stalk (approx)
    "scallion": 12.0,
    "onion": 110.0,
    "apple": 180.0,
    "banana": 120.0,
    "lemon": 60.0,  # juice-ish; prefer juice measures
    "lime": 40.0,
    "egg": 50.0,
    "avocado": 150.0,
}

TIN_G = 400.0

# Fallback if DB missing (same shape as DB rows after load)
_FALLBACK_FOODS: list[tuple[list[str], float | None, str | None, bool]] = [
    (["brown rice pasta", "rice pasta", "penne", "fettuccine", "fettucine", "linguine", "spaghetti", "pasta", "noodle"], 150, "FRT", False),
    (["quinoa"], 155, "FRT", False),
    (["brown rice", "whole grain rice", "wholegrain rice"], 180, None, True),
    (["rice"], 190, None, True),
    (["oat", "rolled oat"], 52, "FRT", False),
    (["wheat", "couscous", "bulgur", "semolina"], 15, "FRT", False),
    (["flour"], 50, "FRT", False),
    (["sundried tomato", "sun-dried tomato", "sun dried tomato"], 13, "FRU", False),
    (["tomato puree", "tomato paste"], 28, "FRT", False),
    (["plum tomato", "chopped tomato", "canned tomato", "tinned tomato"], 100, "FRU", False),
    (["passata"], 72, "FRU", False),
    (["tomato"], 65, "FRU", False),
    (["cherry tomato", "cherry tomatoes"], 45, "FRU", False),
    (["roma tomato", "roma tomatoes"], 48, "FRU", False),
    (["apple"], 20, "FRU", False),
    (["mango"], 40, "FRU", False),
    (["pineapple"], 140, "FRT", False),
    (["honey"], 7, "FRU", False),
    (["agave"], 5, "FRU", False),
    (["green pepper", "green bell", "green bell pepper"], 75, "FRT", False),
    (["red pepper", "red bell", "red bell pepper", "bell pepper", "capsicum"], 43, "FRU", False),
    (["yellow or orange bell pepper", "orange or yellow bell pepper", "yellow or orange", "orange or yellow", "yellow pepper", "yellow bell pepper", "yellow bell"], 35, "FRU", False),
    (["orange pepper", "orange bell", "orange bell pepper"], 38, "FRU", False),
    (["garlic"], 1, "FRT", False),
    (["onion", "shallot"], 10, "FRT", False),
    (["spring onion green tops", "spring onion greens", "spring onion tops", "scallion green tops", "scallion greens", "green onion green tops", "green onion tops", "green onion greens"], 75, "FRU", False),
    (["leek"], 14, "FRT", False),
    (["leek, green tops", "leek green tops", "leek greens", "leek leaves", "green part of 1 leek", "green part of leek", "green tops of leek"], None, None, True),
    (["fennel"], 48, "FRT", False),
    (["cabbage"], 75, "FRT", False),
    (["broccoli florets", "broccoli floret", "broccoli heads", "broccoli head", "broccoli"], 75, "FRU", False),
    (["broccoli stalks", "broccoli stalk", "broccoli stems", "broccoli stem"], 45, "FRU", False),
    (["cauliflower"], 75, "FRT", False),
    (["button mushroom", "portobello", "portabella", "champignon", "mushroom"], 7, "MAN", False),
    (["oyster mushroom", "oyster mushrooms"], 75, None, True),
    (["shiitake"], 7, "MAN", False),
    (["sweet potato"], 75, "MAN", False),
    (["avocado"], 60, "SOR", False),
    (["celery"], 51, "MAN", False),
    (["carrot"], 75, None, True),
    (["spinach"], 150, None, True),
    (["courgette", "zucchini"], 65, "FRT", False),
    (["aubergine", "eggplant"], 75, "SOR", False),
    (["potato"], 200, None, True),
    (["chickpea", "garbanzo"], 42, "GOS", False),
    (["lentil"], 46, "GOS", False),
    (["kidney bean"], 40, "GOS", False),
    (["black bean"], 40, "GOS", False),
    (["edamame"], 75, "GOS", False),
    (["beansprout", "bean sprout"], 75, None, True),
    (["firm tofu", "tofu"], 170, "GOS", False),
    (["silken tofu", "soft tofu"], 39, "GOS", False),
    (["soy milk", "soya milk"], 40, "GOS", False),
    (["almond milk"], 240, None, True),
    (["oat milk"], 104, "FRT", False),
    (["rice milk", "coconut-rice milk", "coconut rice milk"], 200, "FRT", False),
    (["hemp milk", "macadamia milk"], 240, None, True),
    (["plant-based milk", "plant based milk", "non-dairy milk", "nondairy milk", "dairy-free milk", "dairy free milk", "vegetable milk"], 200, None, True),
    (["condensed milk", "evaporated milk", "cow's milk", "cows milk", "whole milk", "skimmed milk", "skim milk", "semi-skimmed milk", "semi skimmed milk", "full-fat milk", "full fat milk", "dairy milk", "buttermilk", "milk"], 40, "LAC", False),
    (["yoghurt", "yogurt"], 40, "LAC", False),
    (["cream"], 40, "LAC", False),
    (["soft cheese", "ricotta", "cottage cheese"], 40, "LAC", False),
    (["parmesan", "parmigiano", "hard cheese", "cheddar"], 40, None, True),
    (["coconut milk"], 60, "SOR", False),
    (["kalamata olive", "olive"], 30, None, True),
]


@dataclass(frozen=True)
class FoodHit:
    """Match against monash_fodmap.sqlite (or fallback)."""

    green_g: float | None
    ftype: str | None
    no_upper_limit: bool


@lru_cache(maxsize=1)
def load_foods() -> list[tuple[list[str], float | None, str | None, bool]]:
    """Return [(match_keys, green_g, ftype, no_upper_limit), ...] from DB."""
    if not MONASH_DB.exists():
        return list(_FALLBACK_FOODS)
    con = sqlite3.connect(MONASH_DB)
    rows = con.execute(
        """
        SELECT f.id, f.green_g, f.fodmap_type, f.no_upper_limit,
               GROUP_CONCAT(k.match_key, char(31))
        FROM foods f
        JOIN food_match_keys k ON k.food_id = f.id
        GROUP BY f.id
        """
    ).fetchall()
    con.close()
    out: list[tuple[list[str], float | None, str | None, bool]] = []
    for _id, green_g, ftype, no_lim, keys_blob in rows:
        keys = [k for k in (keys_blob or "").split("\x1f") if k]
        if not keys:
            continue
        out.append((keys, green_g, ftype, bool(no_lim)))
    return out if out else list(_FALLBACK_FOODS)


def reload_foods() -> None:
    load_foods.cache_clear()


# Back-compat name used by older scripts
FOODS = _FALLBACK_FOODS  # type: ignore[misc]

VULGAR = {"¼": "1/4", "½": "1/2", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}


def emoji_for(ratio: float) -> str:
    if ratio <= 0.25:
        return "🟢"
    if ratio <= 0.50:
        return "🟡"
    if ratio <= 0.75:
        return "🟠"
    return "🔴"


def meal_emoji(ratio: float) -> str:
    """Traffic circles for description type totals (and ingredient-scale uses emoji_for)."""
    if ratio <= 0.5:
        return "🟢"
    if ratio <= 1.0:
        return "🟡"
    if ratio <= 1.5:
        return "🟠"
    return "🔴"


def title_meal_emoji(ratio: float) -> str:
    """Monochrome-friendly 3-step title markers (Windows list greys out coloured circles).

    ✅ ≤50% (good) · ℹ️ ≤150% (yellow+orange middle) · ❌ >150% (bad)
    """
    if ratio <= 0.5:
        return "✅"
    if ratio <= 1.5:
        return "ℹ️"
    return "❌"


def pct(ratio: float) -> int:
    return int(round(ratio * 100))


def format_tag(ftype: str, ratio: float) -> str:
    return f"{emoji_for(ratio)} {TYPE_NAME[ftype]} {pct(ratio)}%"


def format_meal_tag(ftype: str, ratio: float) -> str:
    return f"{meal_emoji(ratio)} {TYPE_NAME[ftype]} {pct(ratio)}%"


def strip_notes_block(notes: str) -> str:
    notes = notes or ""
    notes = re.sub(
        rf"\n*{re.escape(BLOCK_START)}.*?{re.escape(BLOCK_END)}\n*",
        "\n",
        notes,
        flags=re.S,
    )
    notes = re.sub(rf"\n*{re.escape(BLOCK_START)}.*", "", notes, flags=re.S)
    return notes.strip()


def strip_desc_fodmap(desc: str) -> str:
    desc = (desc or "").rstrip()
    desc = re.sub(r"\n*FODMAP:[^\n]*\s*$", "", desc)
    names = "|".join(re.escape(n) for n in TYPE_NAME.values())
    names_ext = names + r"|Excess fructose"
    desc = re.sub(
        rf"\n*(?:🟢|🟡|🟠|🔴)\s+(?:{names_ext})\s+\d+%.*\s*$",
        "",
        desc,
        flags=re.S,
    )
    desc = re.sub(r"\n*🟢 all tracked types near green\s*$", "", desc)
    return desc.rstrip()


def strip_inline_fodmap(line: str) -> str:
    names = "|".join(re.escape(n) for n in TYPE_NAME.values())
    names_ext = names + r"|Excess fructose"
    line = re.sub(rf"\s+(?:🟢|🟡|🟠|🔴)\s+(?:{names_ext})\s+\d+%\s*$", "", line)
    line = re.sub(r"\s+🟢\s+free\s*$", "", line)
    line = re.sub(
        r"\s+(?:FRU|FRT|LAC|GOS|SOR|MAN)\s+(?:🟢|🟡|🟠|🔴)\s+~\d+(?:\.\d+)?×(?:\s+\([^)]*\))?\s*$",
        "",
        line,
    )
    return line.rstrip()


# Title: new set + legacy coloured circles (so re-score replaces either)
_TITLE_EMOJI_RE = re.compile(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*")


def set_title_meal_emoji(name: str, emoji: str) -> str:
    base = _TITLE_EMOJI_RE.sub("", (name or "").strip())
    return f"{emoji} {base}"


def parse_number_token(tok: str) -> float | None:
    tok = tok.strip()
    for k, v in VULGAR.items():
        tok = tok.replace(k, v)
    tok = tok.replace("⁄", "/")
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(int(m.group(1)) + Fraction(int(m.group(2)), int(m.group(3))))
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(Fraction(int(m.group(1)), int(m.group(2))))
    try:
        return float(tok.replace(",", "."))
    except ValueError:
        return None


@dataclass
class ParsedLine:
    value: float
    unit: str | None
    rest: str


_LINE_RE = re.compile(
    r"""^\s*
    (?P<qty>
        (?:[¼½¾⅓⅔⅛]|\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)
        (?:\s*(?:to|-|–|—)\s*(?:[¼½¾⅓⅔⅛]|\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?))?
    )
    \s*
    (?:
        (?P<tin>x\s*(?P<tin_g>\d+)\s*g\s*tins?)
      | (?P<unit>
            kilograms?|kg|grams?|gram|g|
            millilitres?|milliliters?|ml|
            litres?|liters?|l|
            tablespoons?|tbsp|tbs|
            teaspoons?|tsp|
            cups?|cup|
            ounces?|oz|
            pounds?|lbs?|lb|
            medium|large|small
        )
    )?
    \b
    \s*
    (?P<rest>.*)$
    """,
    re.I | re.X,
)


def parse_line(line: str) -> ParsedLine | None:
    s = strip_inline_fodmap(line).strip()
    if not s:
        return None
    m = _LINE_RE.match(s)
    if not m:
        return None
    qty_raw = m.group("qty")
    parts = re.split(r"\s*(?:to|-|–|—)\s*", qty_raw)
    nums = [parse_number_token(p) for p in parts]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None
    value = sum(nums) / len(nums)
    rest = (m.group("rest") or "").strip()
    rest = re.sub(r"^(?:of\s+)", "", rest, flags=re.I)

    if m.group("tin"):
        tin_g = float(m.group("tin_g") or TIN_G)
        return ParsedLine(value=value * tin_g, unit="g", rest=rest)

    unit_raw = (m.group("unit") or "").lower()
    if unit_raw in {"medium", "large", "small"}:
        return ParsedLine(value=value, unit="count", rest=rest)
    unit_map = {
        "kilograms": "kg",
        "kilogram": "kg",
        "kg": "kg",
        "grams": "g",
        "gram": "g",
        "g": "g",
        "millilitres": "ml",
        "milliliters": "ml",
        "ml": "ml",
        "litres": "l",
        "liters": "l",
        "l": "l",
        "tablespoons": "tbsp",
        "tablespoon": "tbsp",
        "tbsp": "tbsp",
        "tbs": "tbsp",
        "teaspoons": "tsp",
        "teaspoon": "tsp",
        "tsp": "tsp",
        "cups": "cup",
        "cup": "cup",
        "ounces": "oz",
        "ounce": "oz",
        "oz": "oz",
        "pounds": "lb",
        "pound": "lb",
        "lbs": "lb",
        "lb": "lb",
    }
    unit = unit_map.get(unit_raw) if unit_raw else None
    if unit is None and not unit_raw:
        # bare number → count
        return ParsedLine(value=value, unit="count", rest=rest)
    return ParsedLine(value=value, unit=unit, rest=rest)


def density_tsp(name: str) -> float:
    n = name.lower()
    for key, g in DENSITY_TSP:
        if key in n:
            return g
    return 5.0


def to_grams(p: ParsedLine) -> float | None:
    rest = p.rest.lower()
    if p.unit == "g":
        return p.value
    if p.unit == "kg":
        return p.value * 1000
    if p.unit == "oz":
        return p.value * 28.35
    if p.unit == "lb":
        return p.value * 453.6
    if p.unit == "ml":
        return p.value
    if p.unit == "l":
        return p.value * 1000
    if p.unit == "tsp":
        return p.value * density_tsp(rest)
    if p.unit == "tbsp":
        return p.value * 3 * density_tsp(rest)
    if p.unit == "cup":
        # liquids ~240 g; pastes denser — use 240 as default kitchen cup
        return p.value * 240.0
    if p.unit == "count":
        # Leek green tops only ≈ 1 cup chopped greens per large leek
        if re.search(r"\bleeks?\b", rest) and re.search(
            r"green\s+(tops?|parts?|leaves)|greens?\s+only|white\s+base\s+discarded",
            rest,
        ):
            return p.value * 75.0
        # Spring onion / scallion green tops — ~12 g greens per stalk
        if re.search(r"spring\s+onions?|scallions?|green\s+onions?", rest) and re.search(
            r"green\s+(tops?|parts?|leaves)|greens?\b", rest
        ):
            return p.value * 12.0
        for key, g in ITEM_G.items():
            if key in rest:
                # avoid black pepper spice
                if key == "pepper" and (
                    "black pepper" in rest or "white pepper" in rest or "ground pepper" in rest
                ):
                    continue
                return p.value * g
        return None
    return None


def match_food(rest: str) -> FoodHit | None:
    n = rest.lower()
    # Normalize compact qualifiers before stripping notes:
    # "(vegetable) milk" / "(almond) milk" → "vegetable milk"
    n_norm = re.sub(
        r"\((vegetable|plant|almond|oat|soy|soya|rice|coconut|hemp|macadamia|"
        r"non[-\s]?dairy|dairy[-\s]?free)\)\s*(milks?)",
        r"\1 \2",
        n,
    )
    # Ignore other parenthetical notes for food identity
    # ("I used coconut-rice milk", "skip if the pineapple is frozen")
    n_keys = re.sub(r"\([^)]*\)", " ", n_norm)
    n_keys = re.sub(r"\s+", " ", n_keys).strip()
    # Garlic-infused oil is low-FODMAP (flavour only) — do not score as garlic
    garlic_infused = bool(
        re.search(r"garlic[-\s]?infused", n)
        or re.search(r"infused\s+.*\bgarlic\b", n)
        or "replaces onion/garlic" in n
        or "replaces garlic" in n
    )
    garlic_negated = bool(
        garlic_infused
        or re.search(r"garlic[-\s]?free", n)
        or re.search(r"(?:no|without)\s+(?:added\s+)?garlic", n)
        or re.search(r"check.{0,60}garlic", n)
        or re.search(r"garlic or onion|onion or garlic|onion,\s*garlic", n)
        or re.search(r"(?:or\s+)?sub(?:stitute)?\s+garlic", n)
        or ("list onion and garlic" in n)
        or ("onion and garlic" in n and "almost always" in n)
    )
    onion_negated = bool(
        re.search(r"onion[-\s]?free", n)
        or re.search(r"(?:no|without)\s+(?:added\s+)?onion", n)
        or re.search(r"check.{0,60}onion", n)
        or re.search(r"garlic or onion|onion or garlic|onion,\s*garlic", n)
        or re.search(r"(?:or\s+)?sub(?:stitute)?\s+(?:garlic\s+and\s+)?onions?", n)
        or ("list onion and garlic" in n)
        or ("onion and garlic" in n and "almost always" in n)
    )
    # Green tops / leaves of alliums — dedicated foods (not bulb onion/leek)
    allium_greens = bool(
        re.search(
            r"green\s+(tops?|parts?|leaves)|greens?\s+only|dark\s+green|white\s+base\s+discarded",
            n,
        )
        or re.search(r"\bgreens\b", n)
    )
    lactose_free = bool(re.search(r"lactose[-\s]?free", n))

    # Prefer green-part entries before generic onion/leek bulb
    if allium_greens and re.search(r"\bleeks?\b", n):
        for keys, green, ftype, no_lim in load_foods():
            if no_lim and any("leek" in k for k in keys):
                return FoodHit(
                    green_g=green, ftype=ftype, no_upper_limit=True
                )
        return FoodHit(green_g=None, ftype=None, no_upper_limit=True)

    if allium_greens and re.search(
        r"spring\s+onions?|scallions?|green\s+onions?", n
    ):
        for keys, green, ftype, no_lim in load_foods():
            if any(
                "spring onion" in k or "scallion" in k or "green onion" in k
                for k in keys
            ):
                return FoodHit(
                    green_g=green, ftype=ftype, no_upper_limit=bool(no_lim)
                )
        return FoodHit(green_g=75.0, ftype="FRU", no_upper_limit=False)

    # skip pure spices / salt / water
    if re.search(r"\b(salt|pepper|paprika|cumin|turmeric|herb|stock|water|oil|vinegar)\b", n):
        if not any(
            k in n
            for k in (
                "tomato",
                "onion",
                "garlic",
                "leek",
                "milk",
                "yoghurt",
                "yogurt",
                "bean",
                "lentil",
                "chickpea",
                "pasta",
                "rice",
                "pepper",
            )
        ) or (garlic_infused and "oil" in n):
            # allow red pepper etc. via foods list; block black pepper / garlic oil
            if "black pepper" in n or "white pepper" in n:
                return None
            if garlic_infused and "oil" in n:
                return None
            if re.match(r"^(sea )?salt|black pepper|mixed herbs", n):
                return None

    best: tuple[int, FoodHit] | None = None
    for keys, green, ftype, no_lim in load_foods():
        for key in keys:
            # Word-boundary match so "apple" does not hit "pineapple", etc.
            if not re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", n_keys):
                continue
            # black/white pepper spice vs capsicum
            if key in {"pepper", "red pepper", "yellow pepper", "orange pepper"} and (
                "black pepper" in n or "white pepper" in n
            ):
                continue
            if key == "garlic" and garlic_negated:
                continue
            if key in {"onion", "shallot"} and (
                onion_negated or allium_greens
            ):
                continue
            # plain "leek" key must not win over greens (handled above)
            if key == "leek" and allium_greens:
                continue
            # olive oil is not olives (but olives packed in oil still count)
            if (
                key in {"olive", "kalamata olive"}
                and "olive oil" in n
                and "olives" not in n
            ):
                continue
            # herb mixes mentioning fennel seed/bulb in a list are not a fennel serve
            if key == "fennel" and "herb" in n:
                continue
            # lactose-free dairy is not a lactose load
            if ftype == "LAC" and lactose_free:
                continue
            # plant / non-dairy milks are never cow-milk lactose (longer keys preferred)
            if key == "milk" and re.search(
                r"\b(almond|oat|soy|soya|rice|coconut|hemp|cashew|pea|macadamia|hazelnut|walnut|"
                r"plant[-\s]?based|non[-\s]?dairy|dairy[-\s]?free|vegetable)\b",
                n_keys,
            ):
                continue
            # milk chocolate is confectionery, not a milk drink serve
            if key == "milk" and re.search(r"\bmilk\s+chocolate\b|\bchocolate\s+milk\b", n_keys):
                continue
            # allergen-only MILK in cheese labels: "Parmigiano (MILK) [MILK Salt...]"
            if key == "milk" and (
                re.search(r"\b(parmesan|parmigiano|pecorino|cheddar|hard cheese)\b", n_keys)
                or (
                    re.search(r"\([^)]*\bmilk\b[^)]*\)|\[[^\]]*\bmilk\b[^\]]*\]", n)
                    and not re.search(
                        r"\b(cup|ml|l|tbsp|tsp|pint|gallon|litre|liter)\b.*\bmilk\b|"
                        r"\bmilk\b.*\b(cup|ml|l|tbsp|tsp)\b",
                        n_keys,
                    )
                )
            ):
                continue
            hit = FoodHit(green_g=green, ftype=ftype, no_upper_limit=bool(no_lim))
            score = len(key)
            if best is None or score > best[0]:
                best = (score, hit)
    return best[1] if best else None


def score_ingredient_line(
    line: str,
) -> tuple[str, float, float | None, str | None, bool] | None:
    """Return (clean, grams, green_g, ftype, no_upper_limit) or None."""
    clean = strip_inline_fodmap(line)
    p = parse_line(clean)
    if not p:
        return None
    food = match_food(p.rest)
    if not food:
        return None
    grams = to_grams(p)
    if grams is None or grams <= 0:
        return None
    return clean, grams, food.green_g, food.ftype, food.no_upper_limit


def annotate_ingredients(ingredients: str) -> tuple[str, dict[str, float]]:
    stacks: dict[str, float] = {}
    out: list[str] = []
    for line in (ingredients or "").splitlines():
        if not line.strip():
            continue
        scored = score_ingredient_line(line)
        if not scored:
            # Untracked — no DB entry (protein, salt, most oils/spices)
            out.append(strip_inline_fodmap(line))
            continue
        clean, grams, green, ftype, no_lim = scored
        if no_lim or not ftype or not green or green <= 0:
            # Monash no-upper-limit / free food — leave blank (not a stacking budget)
            out.append(clean)
            continue
        ratio = grams / green
        stacks[ftype] = stacks.get(ftype, 0.0) + ratio
        out.append(f"{clean}  {format_tag(ftype, ratio)}")
    text = "\n".join(out).rstrip() + ("\n" if out else "")
    return text, stacks


def meal_score_emoji(stacks: dict[str, float]) -> str:
    worst = "✅"
    worst_load = 0.0
    for t in TYPE_ORDER:
        load = stacks.get(t, 0.0)
        if load < 0.5:
            continue
        if load > worst_load:
            worst_load = load
            worst = title_meal_emoji(load)
    return worst


def build_desc_summary(stacks: dict[str, float]) -> str | None:
    parts = []
    for t in TYPE_ORDER:
        load = stacks.get(t, 0.0)
        if load <= 0:
            continue
        parts.append(format_meal_tag(t, load))
    if not parts:
        return None
    return "  ·  ".join(parts)


def transform_recipe(
    name: str, ingredients: str, description: str, notes: str
) -> dict:
    new_ings, stacks = annotate_ingredients(ingredients)
    meal_em = meal_score_emoji(stacks)
    new_name = set_title_meal_emoji(name, meal_em)
    new_notes = strip_notes_block(notes)
    desc_base = strip_desc_fodmap(description)
    summary = build_desc_summary(stacks)
    if summary:
        new_desc = (desc_base + "\n\n" + summary).strip() + "\n" if desc_base else summary + "\n"
    else:
        new_desc = (desc_base + "\n").lstrip("\n") if desc_base else ""
    return {
        "name": new_name,
        "ingredients": new_ings,
        "description": new_desc,
        "notes": new_notes,
        "stacks": {k: round(v, 3) for k, v in stacks.items()},
        "meal_emoji": meal_em,
    }
