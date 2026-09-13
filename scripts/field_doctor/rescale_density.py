"""Rescale FD ingredients; density-aware tsp/tbsp for minor items."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html as html_lib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

_PROJECT = Path(__file__).resolve().parents[2]
ENV_PATH = _PROJECT / ".env"
if not ENV_PATH.is_file():
    ENV_PATH = Path.home() / ".paprika-mcp.env"
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
PORTIONS = 8
MINOR_MAX_G = 45.0

# Approximate grams per level teaspoon (culinary references / USDA-style kitchen densites).
# Matched longest keyword first against lowercased ingredient name.
# Format: (keyword, g_per_tsp)
DENSITY_TABLE: list[tuple[str, float]] = [
    # liquids / sauces
    ("extra virgin olive oil", 4.5),
    ("olive oil", 4.5),
    ("coconut milk", 5.0),
    ("soy milk", 5.0),
    ("fish sauce", 6.0),
    ("tamarind", 5.5),
    ("red wine vinegar", 5.0),
    ("wine vinegar", 5.0),
    ("vinegar", 5.0),
    ("lime juice", 5.0),
    ("lemon juice", 5.0),
    ("red wine", 4.9),
    ("tamari", 6.0),
    ("soy sauce", 6.0),
    ("tomato puree", 5.5),
    ("tomato paste", 5.5),
    ("beef stock", 5.0),
    ("stock", 5.0),
    ("miso", 6.0),
    ("yeast extract", 5.5),
    ("water", 5.0),
    # salts / sugars
    ("smoked sea salt", 5.0),
    ("sea salt", 5.0),
    ("salt", 6.0),
    ("brown sugar", 4.5),
    ("sugar", 4.2),
    # starches / flours
    ("tapioca starch", 2.8),
    ("tapioca", 2.8),
    ("quinoa flour", 2.5),
    ("flour", 2.5),
    # ground spices
    ("black pepper", 2.3),
    ("white pepper", 2.3),
    ("pepper", 2.3),
    ("smoked paprika", 2.3),
    ("sweet noble paprika", 2.3),
    ("paprika", 2.3),
    ("cumin ground", 2.1),
    ("cumin seeds", 2.1),
    ("cumin", 2.1),
    ("coriander ground", 1.8),
    ("garam masala", 2.0),
    ("turmeric blend", 3.0),
    ("turmeric", 3.0),
    ("chipotle", 2.5),
    ("chilli", 2.5),
    ("chili", 2.5),
    ("cayenne", 1.8),
    ("cinnamon", 2.6),
    ("cardamom", 2.0),
    ("fennel ground", 1.8),
    ("fennel seed", 2.0),
    ("mustard", 2.5),
    ("nigella", 2.1),
    ("black onion", 2.1),
    ("onion seed", 2.1),
    ("mixed herbs", 1.0),  # dried mixed herbs ~1 g/tsp
    ("herb mix", 1.0),
    ("italian herb", 1.0),
    # fresh / leafy (chopped volume)
    ("parsley flat", 1.5),  # chopped fresh ~1.5 g/tsp
    ("parsley", 1.5),
    ("coriander", 1.5),  # fresh leaf; if "coriander ground" matched above
    ("chive rings", 1.2),
    ("chive", 1.2),
    ("spinach", 1.5),  # rarely minor
    ("basil", 1.2),
    ("mint", 1.2),
    ("lemongrass", 2.5),  # minced
    ("ginger", 5.0),  # fresh grated/minced ≈ water-ish; dried would be lighter
    # other small solids
    ("sundried tomato", 3.5),
    ("sun-dried tomato", 3.5),
    ("kalamata olive", 4.0),  # chopped/packed
    ("olive", 4.0),
    ("pumpkin seed", 3.0),
    ("italian hard cheese", 2.5),  # grated
    ("cheese", 2.5),
    ("fennel", 3.0),  # if small amount of diced fennel (unusual)
    ("pea", 3.5),
]

DEFAULT_G_PER_TSP = 2.5  # generic dry/spice fallback

# Medium UK supermarket sweet/bell pepper (~120–150 g; midpoint used for counts)
AVG_UK_PEPPER_G = 140.0
SWEET_PEPPER_RE = re.compile(r"(?i)\b(red|green|yellow|orange)\s+peppers?\b")

# Medium UK supermarket carrot (Sainsbury's labels 80 g as one medium / 1 of 5-a-day)
AVG_UK_CARROT_G = 80.0
CARROT_RE = re.compile(r"(?i)\bcarrots?\b")

# Medium UK supermarket sweet potato (~130 g standard medium / pack serving)
AVG_UK_SWEET_POTATO_G = 130.0
SWEET_POTATO_RE = re.compile(r"(?i)\bsweet\s+potato(?:es)?\b")

# Medium UK supermarket courgette (~150–200 g; midpoint)
AVG_UK_COURGETTE_G = 175.0
COURGETTE_RE = re.compile(r"(?i)\b(?:courgettes?|zucchinis?)\b")

# Medium UK supermarket aubergine (~225–300 g each; use 250 g)
AVG_UK_AUBERGINE_G = 250.0
AUBERGINE_RE = re.compile(r"(?i)\b(?:aubergine|eggplant)s?\b")

# Medium UK supermarket swede (~700–1000 g; use 800 g)
AVG_UK_SWEDE_G = 800.0
SWEDE_RE = re.compile(r"(?i)\b(?:swedes?|rutabagas?)\b")

# Standard UK chopped tomato tin (net weight; drained weight is lower but recipes use tin count)
UK_CHOPPED_TOMATO_TIN_G = 400.0
CHOPPED_TOMATO_RE = re.compile(r"(?i)\bchopped\s+tomato(?:es)?\b")

# Juice yield from medium citrus (~3 tbsp / 45 ml per lemon; ~2 tbsp / 30 ml per lime)
JUICE_PER_LEMON_G = 45.0
JUICE_PER_LIME_G = 30.0
LEMON_JUICE_RE = re.compile(r"(?i)\blemon\s+juice\b")
LIME_JUICE_RE = re.compile(r"(?i)\blime\s+juice\b")

# Keep these as grams even when under MINOR_MAX_G (chunky / not spooned)
KEEP_AS_GRAMS = re.compile(
    r"(?i)\b("
    r"fennel(?!\s*(ground|seed|seeds))|"
    r"olives?|sundried|sun-dried|"
    r"peas?|(?<!black\s)beans?|"
    r"mushrooms?|potatoes?|spinach|broccoli|edamame|"
    r"extra\s+lean\s+beef|chicken(?!\s*stock)|salmon|hake|"
    r"tofu|rice|pasta|penne|noodle|\bwater\b|"
    r"quinoa(?!\s*flour)|chives?"
    r")\b"
)


def primary_name(name: str) -> str:
    """Ingredient name only — ignore [composition] / trailing notes for density match."""
    return re.split(r"[\[\({]", name, maxsplit=1)[0].strip()


def display_name(name: str) -> str:
    """Rewrite label names for clearer Paprika display."""
    name = re.sub(r"(?i)^Italian Hard Cheese\b", "Parmigiano", name, count=1)
    # FD labels "Chive" / "Chive Rings" → Chives
    name = re.sub(r"(?i)^Chive Rings\b", "Chives", name, count=1)
    name = re.sub(r"(?i)^Chive\b(?!s)", "Chives", name, count=1)
    name = re.sub(r"(?i)^Herb Mix\b", "Mixed Herbs", name, count=1)
    return name


def g_per_tsp_for(name: str) -> float:
    low = primary_name(name).lower()
    matches = [(kw, g) for kw, g in DENSITY_TABLE if kw in low]
    if not matches:
        return DEFAULT_G_PER_TSP
    matches.sort(key=lambda x: len(x[0]), reverse=True)
    return matches[0][1]


def should_keep_grams(name: str, grams: float) -> bool:
    if grams >= MINOR_MAX_G:
        return True
    # Match against display name too (e.g. Chive Rings → Chives)
    check = display_name(primary_name(name))
    return bool(KEEP_AS_GRAMS.search(check))


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def clean_text(s: str) -> str:
    s = html_lib.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\\u003cb\\u003e", "").replace("\\u003c/b\\u003e", "")
    s = s.replace("Enzymes}", "Enzymes]")
    s = s.replace("Water}", "Water]")
    # Only fix clear FD typo "Salt)" at end of allergen paren — do NOT rewrite "Sugar)"
    s = re.sub(r"\bSalt\)", "Salt]", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_from_html(html: str) -> tuple[str | None, int]:
    m = re.search(r'ingredient_list\\",\\"(.*?)\\"', html)
    if not m:
        m = re.search(r'ingredients" class="prose">(.*?)</', html, re.I | re.S)
    ingredients = clean_text(m.group(1)) if m else None
    if ingredients:
        ingredients = re.split(r"(?i)for allergens|manufactured on a site", ingredients)[0]
        ingredients = clean_text(ingredients).strip(" ,.")
    servings = [int(x) for x in re.findall(r"per (\d{2,4})g", html, re.I)]
    non100 = [s for s in servings if s != 100]
    serving = max(non100) if non100 else (max(servings) if servings else 400)
    return ingredients, serving


@dataclass
class Ing:
    name: str
    pct: float | None
    assigned: float = 0.0


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


def parse_ingredients(raw: str) -> list[Ing]:
    raw = clean_text(raw)
    parts = split_top_level(raw)
    expanded: list[str] = []
    for part in parts:
        embedded = list(re.finditer(r"\((\d+(?:\.\d+)?)\s*%\)", part))
        if len(embedded) <= 1:
            expanded.append(part)
            continue
        start = 0
        for m in embedded:
            chunk = part[start : m.end()].strip(" ,.")
            if chunk:
                expanded.append(chunk)
            start = m.end()
        rest = part[start:].strip(" ,.")
        if rest:
            expanded.extend(split_top_level(rest))

    items: list[Ing] = []
    for part in expanded:
        part = part.strip(" ,.")
        if not part:
            continue
        m = re.search(r"^(.*?)(?:\((\d+(?:\.\d+)?)\s*%\))\s*$", part)
        if m:
            name, pct = m.group(1).strip(" ,."), float(m.group(2))
        else:
            name, pct = part, None
        name = re.sub(r"\s+", " ", name).strip()
        if not name or re.fullmatch(r"microbial enzymes\]?", name, re.I):
            continue
        items.append(Ing(name=name, pct=pct))
    return items


def allocate_percentages(items: list[Ing]) -> list[Ing]:
    n = len(items)
    if not n:
        return items

    upper = [100.0] * n
    lower = [0.0] * n
    for i, it in enumerate(items):
        if it.pct is None:
            continue
        for j in range(i):
            lower[j] = max(lower[j], it.pct)
        for j in range(i + 1, n):
            upper[j] = min(upper[j], it.pct)
        it.assigned = it.pct

    remaining = max(0.0, 100.0 - sum(it.assigned for it in items if it.pct is not None))

    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if items[i].pct is None:
            j = i
            while j < n and items[j].pct is None:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1

    run_meta = []
    for a, b in runs:
        k = b - a + 1
        hi = upper[a]
        if a > 0:
            hi = min(hi, items[a - 1].assigned if items[a - 1].assigned else upper[a])
        lo = lower[b]
        if b + 1 < n and items[b + 1].pct is not None:
            lo = max(lo, items[b + 1].pct)
        weight = max(0.05, (hi + max(lo, 0.05)) / 2) * k
        run_meta.append({"a": a, "b": b, "k": k, "hi": hi, "lo": lo, "weight": weight})

    tw = sum(r["weight"] for r in run_meta) or 1.0
    for r in run_meta:
        r["mass"] = remaining * r["weight"] / tw

    for r in run_meta:
        a, b, k, hi, lo, mass = r["a"], r["b"], r["k"], r["hi"], r["lo"], r["mass"]
        mass = min(k * hi, max(k * lo, mass))

        if k == 1:
            items[a].assigned = min(hi, max(lo, mass))
            continue

        best_seq = None
        for pct_r in range(55, 100):
            ratio = pct_r / 100.0
            a0 = mass / k if abs(ratio - 1) < 1e-9 else mass * (1 - ratio) / (1 - ratio**k)
            seq = [a0 * (ratio**j) for j in range(k)]
            if seq[0] > hi + 1e-6 or seq[-1] + 1e-6 < lo:
                continue
            if any(seq[j] < seq[j + 1] - 1e-9 for j in range(k - 1)):
                continue
            best_seq = seq
            break
        if best_seq is None:
            if hi < lo:
                hi = lo
            best_seq = [hi - (hi - lo) * j / (k - 1) for j in range(k)]
            s = sum(best_seq) or 1.0
            best_seq = [x * mass / s for x in best_seq]

        for j, idx in enumerate(range(a, b + 1)):
            val = best_seq[j]
            val = min(val, hi if j == 0 else items[a + j - 1].assigned)
            val = max(val, lo if j == k - 1 else 0.05)
            val = min(val, upper[idx])
            val = max(val, lower[idx], 0.05)
            items[idx].assigned = val

        for j in range(1, k):
            idx = a + j
            if items[idx].assigned > items[a + j - 1].assigned:
                items[idx].assigned = items[a + j - 1].assigned

    for i in range(1, n):
        if items[i].assigned > items[i - 1].assigned:
            items[i].assigned = items[i - 1].assigned

    known_total = sum(it.assigned for it in items if it.pct is not None)
    unk = [it for it in items if it.pct is None]
    unk_sum = sum(it.assigned for it in unk)
    target = max(0.0, 100.0 - known_total)
    if unk and unk_sum > 1e-9:
        scale = target / unk_sum
        for it in unk:
            it.assigned *= scale
    for i in range(1, n):
        if items[i].assigned > items[i - 1].assigned:
            items[i].assigned = items[i - 1].assigned
    return items


def ingredient_merge_key(name: str) -> str:
    """Normalize name for duplicate detection (ignore composition / allergens)."""
    key = primary_name(display_name(name)).lower()
    key = re.sub(r"\s+", " ", key).strip()
    if key.endswith("oes"):
        pass
    elif key.endswith("ies"):
        key = key[:-3] + "y"
    elif key.endswith("s") and not key.endswith("ss"):
        key = key[:-1]
    return key


def is_plain_water(name: str) -> bool:
    """True for standalone Water ingredient (not water inside another product)."""
    return bool(re.fullmatch(r"(?i)water", primary_name(name)))


def is_olive_oil(name: str) -> bool:
    primary = primary_name(name).lower()
    return "olive oil" in primary or primary in {"evoo", "extra virgin olive oil"}


# Keep oil as a listed ingredient when it's a substantial part of the finished dish
# (~120 g / 8 portions ≈ 9 tbsp, or pesto-style sauce oil).
OIL_KEEP_MIN_G_PER_BATCH = 120.0


def should_keep_olive_oil(
    it: Ing, *, serving_g: int, portions: int, recipe_name: str
) -> bool:
    g = serving_g * portions * it.assigned / 100.0
    name = (recipe_name or "").lower()
    if "pesto" in name:
        return True
    if it.pct is not None and it.pct >= 4.0:
        return True
    return g >= OIL_KEEP_MIN_G_PER_BATCH


HOME_LIQUID_TIP = (
    "Liquid for home cooking: cook rice, quinoa, pasta, noodles and potatoes in water as you normally "
    "would (salt the water; drain where appropriate). For sauces, braises and stir-fry glazes, add water "
    "or stock gradually until the consistency is right — do not try to measure the ready-meal's residual water."
)

HOME_OIL_TIP = (
    "Oil for home cooking: where Extra Virgin Olive Oil was only a cooking fat on the label, it is omitted "
    "from ingredients — fry, soften, sear or roast in extra virgin olive oil as needed. "
    "It is kept as an ingredient on richer/oil-forward dishes (e.g. pesto-style sauces or high oil content)."
)

LOW_FODMAP_TIP = (
    "Low FODMAP: no onion or garlic bulbs — use chives / green tops only. "
    "Inspired recreation from Field Doctor ingredients, not an official recipe."
)


def strip_home_liquid_tip(directions: str) -> str:
    """Remove home-cooking liquid tip from directions if present."""
    text = (directions or "").strip()
    text = re.sub(
        r"Liquid for home cooking:.*?(?:\n\n|$)",
        "",
        text,
        count=1,
        flags=re.S,
    ).strip()
    return text


def strip_low_fodmap_tip(directions: str) -> str:
    """Remove Low FODMAP / inspired-recreation blurb from directions; keep storage lines."""
    text = (directions or "").strip()
    text = re.sub(
        r"\n*Low FODMAP:\s*no onion or garlic bulbs.*?use chives\s*/\s*green tops only\.\s*"
        r"(?:Inspired recreation from Field Doctor ingredients, not an official recipe\.\s*)?",
        "\n",
        text,
        flags=re.I | re.S,
    )
    # Orphan fragment if a prior partial strip left it behind
    text = re.sub(
        r"\n*[—\-–�]*\s*use chives\s*/\s*green tops only\.\s*",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\n*Inspired recreation from Field Doctor ingredients, not an official recipe\.\s*",
        "\n",
        text,
        flags=re.I,
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_directions(directions: str) -> str:
    return strip_low_fodmap_tip(strip_home_liquid_tip(directions))


def ensure_fry_in_evoo(directions: str) -> str:
    """Make frying/softening language use extra virgin olive oil when oil was omitted."""
    text = (directions or "").strip()
    if not text:
        return text

    # Upgrade generic olive oil → extra virgin olive oil (avoid double-prefixing)
    text = re.sub(
        r"(?i)(?<!extra virgin )(?<!extra-virgin )\bolive oil\b",
        "extra virgin olive oil",
        text,
    )

    if re.search(r"(?i)extra virgin olive oil", text):
        return text

    tip = (
        "Cook in extra virgin olive oil: fry, soften, sear or roast ingredients "
        "in extra virgin olive oil as needed."
    )
    return tip + "\n\n" + text


def merge_duplicate_ingredients(items: list[Ing]) -> list[Ing]:
    """Keep highest-amount occurrence of each ingredient; drop smaller duplicates."""
    best_idx: dict[str, int] = {}
    for i, it in enumerate(items):
        key = ingredient_merge_key(it.name)
        if key not in best_idx:
            best_idx[key] = i
            continue
        j = best_idx[key]
        cur, prev = items[i], items[j]
        # Prefer higher assigned %; tie-break: labelled pct, then earlier list position
        take_current = False
        if cur.assigned > prev.assigned + 1e-9:
            take_current = True
        elif abs(cur.assigned - prev.assigned) <= 1e-9:
            if cur.pct is not None and prev.pct is None:
                take_current = True
        if take_current:
            best_idx[key] = i

    keep = set(best_idx.values())
    return [it for i, it in enumerate(items) if i in keep]


def fmt_g(grams: float, *, for_display: bool = True) -> str:
    """Format grams. Display amounts >50g round to nearest 5g."""
    if for_display and grams > 50:
        grams = 5 * round(grams / 5)
    if grams < 10:
        return f"{grams:.1f} g"
    return f"{int(round(grams))} g"


SPOON_FRACTIONS = [
    0.125, 0.25, 0.333, 0.5, 0.666, 0.75,
    1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 9, 10,
    12, 15, 18, 21, 24, 27, 30,
]

FRACTION_LABELS = {
    0.125: "1/8",
    0.25: "1/4",
    0.333: "1/3",
    0.5: "1/2",
    0.666: "2/3",
    0.75: "3/4",
    1.0: "1",
    1.25: "1 1/4",
    1.5: "1 1/2",
    1.75: "1 3/4",
    2.0: "2",
    2.25: "2 1/4",
    2.5: "2 1/2",
    2.75: "2 3/4",
    3.0: "3",
    3.5: "3 1/2",
    4.0: "4",
    4.5: "4 1/2",
    5.0: "5",
    6.0: "6",
    7.0: "7",
    8.0: "8",
    9.0: "9",
    10.0: "10",
    12.0: "12",
    15.0: "15",
    18.0: "18",
    21.0: "21",
    24.0: "24",
    27.0: "27",
    30.0: "30",
}


def format_spoon(n: float) -> str:
    key = min(FRACTION_LABELS.keys(), key=lambda k: abs(k - n))
    return FRACTION_LABELS[key]


def format_count_item(
    grams: float,
    name: str,
    unit_g: float,
    singular: str,
    plural: str,
    note_label: str,
) -> tuple[str, str]:
    """Convert grams to a kitchen count of whole items."""
    n = grams / unit_g
    candidates = [
        0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 9, 10,
    ]
    best = min(candidates, key=lambda c: abs(c - n))
    if best < 0.25:
        best = 0.25
    qty = format_spoon(best)
    word = singular if abs(best - 1.0) < 1e-9 else plural
    # Prefer original primary name casing when it already matches
    base = primary_name(name)
    if base.lower() in (singular.lower(), plural.lower()):
        display = f"{qty} {base}" if abs(best - 1.0) < 1e-9 else (
            f"{qty} {base}" if base.lower().endswith("s") else f"{qty} {base}s"
        )
    else:
        display = f"{qty} {word}"
    note = (
        f"{fmt_g(grams, for_display=False)}; "
        f"count from ~{int(unit_g)} g {note_label}"
    )
    return display, note


def format_pepper_count(grams: float, name: str) -> tuple[str, str]:
    base = primary_name(name)
    if base.lower().endswith("peppers"):
        base = base[:-1]
    singular, plural = base, f"{base}s"
    return format_count_item(
        grams,
        name,
        AVG_UK_PEPPER_G,
        singular,
        plural,
        "medium UK supermarket pepper",
    )


def format_carrot_count(grams: float, name: str) -> tuple[str, str]:
    return format_count_item(
        grams,
        name,
        AVG_UK_CARROT_G,
        "medium Carrot",
        "medium Carrots",
        "medium UK supermarket carrot",
    )


def format_sweet_potato_count(grams: float, name: str) -> tuple[str, str]:
    return format_count_item(
        grams,
        name,
        AVG_UK_SWEET_POTATO_G,
        "medium Sweet Potato",
        "medium Sweet Potatoes",
        "medium UK supermarket sweet potato",
    )


def format_courgette_count(grams: float, name: str) -> tuple[str, str]:
    return format_count_item(
        grams,
        name,
        AVG_UK_COURGETTE_G,
        "medium Courgette",
        "medium Courgettes",
        "medium UK supermarket courgette",
    )


def format_aubergine_count(grams: float, name: str) -> tuple[str, str]:
    return format_count_item(
        grams,
        name,
        AVG_UK_AUBERGINE_G,
        "medium Aubergine",
        "medium Aubergines",
        "medium UK supermarket aubergine",
    )


def format_swede_count(grams: float, name: str) -> tuple[str, str]:
    return format_count_item(
        grams,
        name,
        AVG_UK_SWEDE_G,
        "medium Swede",
        "medium Swedes",
        "medium UK supermarket swede",
    )


def format_chopped_tomato_tins(grams: float, name: str) -> tuple[str, str]:
    n = grams / UK_CHOPPED_TOMATO_TIN_G
    # Tin fractions cooks actually use
    candidates = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3, 3.5, 4, 4.5, 5]
    best = min(candidates, key=lambda c: abs(c - n))
    if best < 0.25:
        best = 0.25
    qty = format_spoon(best)
    tin_word = "tin" if abs(best - 1.0) < 1e-9 else "tins"
    display = f"{qty} x 400 g {tin_word} Plum Tomato"
    note = (
        f"{fmt_g(grams, for_display=False)}; "
        f"count from standard UK {int(UK_CHOPPED_TOMATO_TIN_G)} g tin"
    )
    return display, note


def format_citrus_juice(grams: float, fruit: str, juice_per_fruit_g: float) -> tuple[str, str]:
    """Convert lemon/lime juice grams to Paprika-friendly '1/2 Lemon Juice'."""
    n = grams / juice_per_fruit_g
    candidates = [
        0.125, 0.25, 0.333, 0.5, 0.666, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5, 4, 5,
    ]
    best = min(candidates, key=lambda c: abs(c - n))
    if best < 0.125:
        best = 0.125
    qty = format_spoon(best)
    # Quantity first so Paprika parses it; Title Case to match other ingredients
    label = f"{fruit.title()} Juice"
    display = f"{qty} {label}"
    note = (
        f"{fmt_g(grams, for_display=False)}; "
        f"~{int(juice_per_fruit_g)} ml juice per medium {fruit}"
    )
    return display, note


def nearest_kitchen_amount(grams: float, g_per_tsp: float) -> tuple[str, str]:
    tsp = grams / g_per_tsp
    if grams < 0.6 or tsp < 0.15:
        return "pinch", "of"

    best = min(SPOON_FRACTIONS, key=lambda c: abs(c - tsp))

    # Prefer tbsp when >= ~2.5 tsp
    if best >= 2.5:
        tbsp = best / 3.0
        tbsp_candidates = [
            0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 9, 10,
        ]
        best_tbsp = min(tbsp_candidates, key=lambda c: abs(c - tbsp))
        if abs(best_tbsp * 3 - best) <= 0.6 or best >= 3:
            return format_spoon(best_tbsp), "tbsp"

    return format_spoon(best), "tsp"


def format_amount(grams: float, name: str) -> tuple[str, str | None, float | None]:
    """Return (display, grams_note, g_per_tsp_used_or_None)."""
    name = display_name(name)
    primary = primary_name(name)
    if SWEET_PEPPER_RE.search(primary):
        display, note = format_pepper_count(grams, name)
        return display, note, None
    if CARROT_RE.search(primary):
        display, note = format_carrot_count(grams, name)
        return display, note, None
    if SWEET_POTATO_RE.search(primary):
        display, note = format_sweet_potato_count(grams, name)
        return display, note, None
    if COURGETTE_RE.search(primary):
        display, note = format_courgette_count(grams, name)
        return display, note, None
    if AUBERGINE_RE.search(primary):
        display, note = format_aubergine_count(grams, name)
        return display, note, None
    if SWEDE_RE.search(primary):
        display, note = format_swede_count(grams, name)
        return display, note, None
    if CHOPPED_TOMATO_RE.search(primary):
        display, note = format_chopped_tomato_tins(grams, name)
        return display, note, None
    if LEMON_JUICE_RE.search(primary):
        display, note = format_citrus_juice(grams, "lemon", JUICE_PER_LEMON_G)
        return display, note, None
    if LIME_JUICE_RE.search(primary):
        display, note = format_citrus_juice(grams, "lime", JUICE_PER_LIME_G)
        return display, note, None

    if should_keep_grams(name, grams):
        return f"{fmt_g(grams)} {name}", None, None

    gpt = g_per_tsp_for(name)
    qty, unit = nearest_kitchen_amount(grams, gpt)
    if unit == "of":
        return f"pinch of {name}", fmt_g(grams, for_display=False), gpt
    return f"{qty} {unit} {name}", fmt_g(grams, for_display=False), gpt


def build_ingredient_text(items: list[Ing], serving_g: int, portions: int) -> str:
    """Clean Paprika ingredient lines only (quantity + name)."""
    total_g = serving_g * portions
    lines: list[str] = []
    for it in items:
        g = total_g * it.assigned / 100.0
        amount, _, _ = format_amount(g, it.name)
        lines.append(amount)
    return "\n".join(lines)


def build_scaling_notes(
    items: list[Ing],
    serving_g: int,
    portions: int,
    *,
    omitted_water: Ing | None = None,
    omitted_oil: Ing | None = None,
) -> str:
    """Methodology + per-ingredient scaling detail for the Notes field."""
    total_g = serving_g * portions
    lines = [
        "Scaling notes",
        f"Scaled for {portions} portions from Field Doctor single serve (~{serving_g} g x {portions} = {total_g} g).",
        "Unlabelled amounts inferred from UK descending-weight ingredient order "
        "(each item <= the one above and >= the one below; leftover mass shared on that basis, not equally).",
        "Spoon measures use ingredient density (g per tsp) then round to kitchen fractions.",
        "Amounts above 50 g are rounded to the nearest 5 g for cooking convenience.",
        f"Sweet peppers ~{int(AVG_UK_PEPPER_G)} g each; carrots ~{int(AVG_UK_CARROT_G)} g; "
        f"sweet potatoes ~{int(AVG_UK_SWEET_POTATO_G)} g; courgettes ~{int(AVG_UK_COURGETTE_G)} g; "
        f"aubergines ~{int(AVG_UK_AUBERGINE_G)} g; swedes ~{int(AVG_UK_SWEDE_G)} g; "
        f"plum tomatoes = {int(UK_CHOPPED_TOMATO_TIN_G)} g tins; "
        f"lemon ~{int(JUICE_PER_LEMON_G)} ml / lime ~{int(JUICE_PER_LIME_G)} ml juice.",
        "Duplicate label ingredients merged (higher amount kept).",
        HOME_LIQUID_TIP,
        LOW_FODMAP_TIP,
        "Chives listed in grams to match fresh packs (e.g. Waitrose Cooks' Ingredients Chives, 20 g).",
    ]
    if omitted_water is not None:
        wg = total_g * omitted_water.assigned / 100.0
        how = (
            f"{omitted_water.pct:g}% on label"
            if omitted_water.pct is not None
            else f"inferred ~{omitted_water.assigned:.2f}% from list order"
        )
        lines.append(
            f"Ready-meal residual water was ~{fmt_g(wg, for_display=False)} ({how}) and is not listed as an ingredient."
        )
    if omitted_oil is not None:
        og = total_g * omitted_oil.assigned / 100.0
        how = (
            f"{omitted_oil.pct:g}% on label"
            if omitted_oil.pct is not None
            else f"inferred ~{omitted_oil.assigned:.2f}% from list order"
        )
        lines.append(HOME_OIL_TIP)
        lines.append(
            f"Ready-meal residual Extra Virgin Olive Oil was ~{fmt_g(og, for_display=False)} ({how}) "
            f"and is treated as cooking fat (fry in extra virgin olive oil) rather than a measured ingredient."
        )
    elif any(is_olive_oil(it.name) for it in items):
        lines.append(
            "Extra Virgin Olive Oil is kept as an ingredient on this recipe because the ready meal is relatively oil-rich "
            "(or pesto-style); still fry/roast in extra virgin olive oil as directed."
        )
    lines.extend(["", "Ingredient detail"])
    for it in items:
        g = total_g * it.assigned / 100.0
        amount, g_note, gpt = format_amount(g, it.name)
        if it.pct is not None:
            tag = f"{it.pct:g}% on label"
        else:
            tag = f"inferred ~{it.assigned:.2f}% from list order"
        if g_note:
            tag = f"{tag}; {g_note}"
            if gpt is not None:
                tag = f"{tag} @ ~{gpt:g} g/tsp"
        else:
            tag = f"{tag}; {fmt_g(g, for_display=False)}"
        lines.append(f"{amount} — {tag}")
    return "\n".join(lines)


def merge_recipe_notes(existing: str, scaling_notes: str, source_raw: str) -> str:
    """Keep non-scaling notes; replace scaling + source blocks."""
    notes = (existing or "").strip()
    # Drop previous auto scaling / source blocks if present
    notes = re.sub(
        r"\n*Scaling notes\n.*?(?=\n*Source ingredients:|\Z)",
        "\n",
        notes,
        flags=re.S,
    )
    notes = re.sub(r"\n*Source ingredients:.*", "", notes, flags=re.S)
    notes = notes.strip()
    parts = []
    if notes:
        parts.append(notes)
    parts.append(scaling_notes)
    parts.append(f"Source ingredients: {source_raw}")
    return "\n\n".join(parts)


async def main() -> None:
    load_env()
    user = os.environ["PAPRIKA_USERNAME"]
    password = os.environ["PAPRIKA_PASSWORD"]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={"email": user, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        updated = 0
        sample = None
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            url = recipe.get("source_url")
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            raw, serving = extract_from_html(html)
            if not raw:
                print("SKIP", recipe.get("name"))
                continue

            items = merge_duplicate_ingredients(
                allocate_percentages(parse_ingredients(raw))
            )
            omitted_water = next((it for it in items if is_plain_water(it.name)), None)
            items = [it for it in items if not is_plain_water(it.name)]

            rname = recipe.get("name") or ""
            omitted_oil = None
            kept_oils: list[Ing] = []
            rebuilt: list[Ing] = []
            for it in items:
                if not is_olive_oil(it.name):
                    rebuilt.append(it)
                    continue
                if should_keep_olive_oil(
                    it, serving_g=serving, portions=PORTIONS, recipe_name=rname
                ):
                    rebuilt.append(it)
                    kept_oils.append(it)
                else:
                    if omitted_oil is None or it.assigned > omitted_oil.assigned:
                        omitted_oil = it
            items = rebuilt

            text = build_ingredient_text(items, serving, PORTIONS)
            scaling = build_scaling_notes(
                items,
                serving,
                PORTIONS,
                omitted_water=omitted_water,
                omitted_oil=omitted_oil,
            )
            oil_status = (
                "kept"
                if kept_oils
                else ("omitted" if omitted_oil else "none")
            )
            print("OK", rname[:48], "| oil", oil_status)

            recipe["ingredients"] = text
            dirs = clean_directions(recipe.get("directions") or "")
            if omitted_oil is not None:
                dirs = ensure_fry_in_evoo(dirs)
            recipe["directions"] = dirs
            recipe["notes"] = merge_recipe_notes(recipe.get("notes") or "", scaling, raw)
            recipe["categories"] = [FD, LF]
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe["hash"] = calc_hash(recipe)

            form = aiohttp.FormData()
            form.add_field(
                "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
            )
            async with session.post(
                f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
            ) as r:
                if '"result":true' in (await r.text()).replace(" ", ""):
                    updated += 1

            if "Bolognese" in (recipe.get("name") or ""):
                sample = text

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("updated", updated)
        if sample:
            (_PROJECT / "docs" / "fd_bolognese_density.txt").write_text(
                sample, encoding="utf-8"
            )


if __name__ == "__main__":
    asyncio.run(main())
