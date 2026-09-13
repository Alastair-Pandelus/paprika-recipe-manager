"""Cleaner cupboard staple extract across Field Doctor-Style recipes."""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
OUT_JSON = Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_cupboard_staples.json")
OUT_TXT = Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_cupboard_staples.txt")

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

QTY = re.compile(
    r"""(?ix)^\s*
    (?:pinch\s+of\s+)?
    (?:
      \d+\s+\d+/\d+
      |\d+/\d+
      |\d+(?:\.\d+)?
    )
    \s*
    (?:x\s+)?
    (?:
      (?:medium|large|small|heaped|level)\s+
    )?
    (?:
      (?:g|kg|ml|l|tsp|tbsp|teaspoon|tablespoon|cup|cups|
         tin|tins|clove|cloves|pack|packs|pinch|pinches|
         handful|handfuls|slice|slices|sprig|sprigs|
         leaf|leaves|stick|sticks|bunch|bunches)
      \s+
    )?
    (?:
      (?:x\s*)?\d+\s*g\s*(?:tin|tins)?\s*
    )?
    """
)

ALIASES = {
    "pepper": "black pepper",
    "black pepper": "black pepper",
    "ground black pepper": "black pepper",
    "sea salt": "sea salt",
    "salt": "sea salt",
    "himalayan salt": "himalayan / smoked salt",
    "smoked sea salt": "himalayan / smoked salt",
    "tapioca starch": "tapioca flour / starch",
    "tapioca flour": "tapioca flour / starch",
    "tomato puree": "tomato puree",
    "tomato paste": "tomato puree",
    "mixed herbs": "mixed herbs",
    "herb mix": "mixed herbs",
    "sweet noble paprika": "paprika (smoked / sweet noble)",
    "smoked paprika": "paprika (smoked / sweet noble)",
    "sweet paprika": "paprika (smoked / sweet noble)",
    "paprika": "paprika (smoked / sweet noble)",
    "tamari": "tamari / soy sauce",
    "soy sauce": "tamari / soy sauce",
    "extra virgin olive oil": "extra virgin olive oil",
    "olive oil": "extra virgin olive oil",
    "chives": "chives (fresh pack)",
    "chive": "chives (fresh pack)",
    "chive rings": "chives (fresh pack)",
    "coriander": "coriander (ground)",
    "ground coriander": "coriander (ground)",
    "coriander ground": "coriander (ground)",
    "cumin": "cumin (ground)",
    "cumin ground": "cumin (ground)",
    "ground cumin": "cumin (ground)",
    "cumin seeds": "cumin seeds (whole)",
    "cumin seeds whole": "cumin seeds (whole)",
    "ginger": "ginger (ground)",
    "ground ginger": "ginger (ground)",
    "cinnamon ground": "cinnamon (ground)",
    "ground cinnamon": "cinnamon (ground)",
    "cinnamon": "cinnamon (ground)",
    "nutmeg ground": "nutmeg (ground)",
    "ground nutmeg": "nutmeg (ground)",
    "nutmeg": "nutmeg (ground)",
    "turmeric": "turmeric",
    "turmeric blend": "turmeric",
    "ground turmeric": "turmeric",
    "oregano": "oregano (dried)",
    "dried oregano": "oregano (dried)",
    "fennel ground": "fennel (ground)",
    "ground fennel": "fennel (ground)",
    "chipotle chilli": "chipotle chilli",
    "chilli powder": "chilli powder",
    "celery salt": "celery salt",
    "celery salt celery": "celery salt",
    "red wine vinegar": "red wine vinegar",
    "soy milk": "soy milk",
    "coconut milk": "coconut milk",
    "organic coconut milk powder": "coconut milk powder",
    "organic brown rice syrup": "brown rice syrup",
    "brown rice syrup": "brown rice syrup",
    "brown sugar": "brown sugar",
    "oat flour gluten free": "oat flour (GF)",
    "oat flour": "oat flour (GF)",
    "quinoa flour": "quinoa flour",
    "white miso": "white miso",
    "yeast extract": "yeast extract",
    "garam masala": "garam masala",
    "green cardamon ground": "cardamom (ground)",
    "cardamom": "cardamom (ground)",
    "dried fenugreek ground": "fenugreek (ground)",
    "curry leaves": "curry leaves",
    "lemongrass": "lemongrass",
    "tamarind extract": "tamarind extract",
    "saffron": "saffron",
    "basil": "basil (dried / fresh)",
    "cocoa powder": "cocoa powder",
    "cocoa low fat": "cocoa powder",
    "coffee powder": "coffee powder",
    "vanilla powder": "vanilla (powder / extract)",
    "pure vanilla extract": "vanilla (powder / extract)",
    "vanilla extract": "vanilla (powder / extract)",
    "banana powder": "banana powder / oil",
    "pure banana oil": "banana powder / oil",
    "pure lemon oil": "lemon oil",
    "parmigiano": "parmesan / parmigiano",
    "parmesan": "parmesan / parmigiano",
}

CATEGORY = {
    "sea salt": "Salt & pepper",
    "himalayan / smoked salt": "Salt & pepper",
    "black pepper": "Salt & pepper",
    "celery salt": "Salt & pepper",
    "tapioca flour / starch": "Thickeners & flours",
    "oat flour (GF)": "Thickeners & flours",
    "quinoa flour": "Thickeners & flours",
    "cornflour": "Thickeners & flours",
    "mixed herbs": "Herbs (dried)",
    "oregano (dried)": "Herbs (dried)",
    "basil (dried / fresh)": "Herbs (dried)",
    "curry leaves": "Herbs (dried)",
    "chives (fresh pack)": "Herbs (fresh / pack)",
    "paprika (smoked / sweet noble)": "Spices",
    "coriander (ground)": "Spices",
    "cumin (ground)": "Spices",
    "cumin seeds (whole)": "Spices",
    "ginger (ground)": "Spices",
    "cinnamon (ground)": "Spices",
    "nutmeg (ground)": "Spices",
    "turmeric": "Spices",
    "fennel (ground)": "Spices",
    "chipotle chilli": "Spices",
    "chilli powder": "Spices",
    "garam masala": "Spices",
    "cardamom (ground)": "Spices",
    "fenugreek (ground)": "Spices",
    "saffron": "Spices",
    "tomato puree": "Condiments & sauces",
    "tamari / soy sauce": "Condiments & sauces",
    "red wine vinegar": "Condiments & sauces",
    "white miso": "Condiments & sauces",
    "yeast extract": "Condiments & sauces",
    "tamarind extract": "Condiments & sauces",
    "lemongrass": "Condiments & sauces",
    "extra virgin olive oil": "Oils",
    "soy milk": "Plant milks & liquids",
    "coconut milk": "Plant milks & liquids",
    "coconut milk powder": "Plant milks & liquids",
    "brown sugar": "Sweeteners",
    "brown rice syrup": "Sweeteners",
    "cocoa powder": "Baking / bar extras",
    "coffee powder": "Baking / bar extras",
    "vanilla (powder / extract)": "Baking / bar extras",
    "banana powder / oil": "Baking / bar extras",
    "lemon oil": "Baking / bar extras",
    "parmesan / parmigiano": "Cheese (hard)",
}

# Explicit cupboard allowlist after normalize — only these (and CATEGORY keys) count
CUPBOARD_KEYS = set(CATEGORY.keys())


def strip_qty(line: str) -> str:
    s = line.strip()
    if not s:
        return ""
    # multi-item pinches dumped as one line — skip
    if s.lower().startswith("pinch of") and "," in s:
        return ""
    prev = None
    while prev != s:
        prev = s
        s = QTY.sub("", s, count=1).strip()
    s = re.sub(r"\s*\[[^\]]*\]\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,-")
    return s


def normalize(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\(.*?\)", " ", n)
    n = n.replace("&", " and ")
    n = re.sub(r"[^a-z0-9\s/+-]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # drop leading leftover qty words
    n = re.sub(r"^(?:of\s+)+", "", n)
    if n in ALIASES:
        return ALIASES[n]
    # fuzzy: remove trailing allergen words already stripped
    for k, v in ALIASES.items():
        if n == k or n.startswith(k + " "):
            return v
    return n


def short_recipe(name: str) -> str:
    return (
        name.replace("Field Doctor-Style ", "")
        .replace(" (Low FODMAP)", "")
        .strip()
    )


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
        ) as r:
            tok = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {tok}"}
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=H) as r:
            index = (await r.json())["result"]

        recipes = []
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=H
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if recipe.get("in_trash"):
                continue
            if "Field Doctor-Style" not in (recipe.get("name") or ""):
                continue
            recipes.append(recipe)

        buckets: dict[str, dict] = {}
        unknown_spicey = defaultdict(set)

        for recipe in recipes:
            rname = short_recipe(recipe["name"])
            seen = set()
            for line in (recipe.get("ingredients") or "").splitlines():
                raw = strip_qty(line)
                if not raw:
                    continue
                key = normalize(raw)
                if not key or key in seen:
                    continue
                seen.add(key)
                if key in CUPBOARD_KEYS:
                    b = buckets.setdefault(
                        key, {"key": key, "category": CATEGORY[key], "recipes": set(), "labels": set()}
                    )
                    b["recipes"].add(rname)
                    b["labels"].add(raw)
                elif re.search(
                    r"(?i)\b(powder|ground|dried|flakes|starch|flour|vinegar|tamari|paprika|cumin|herb|spice|salt|pepper|miso|syrup|oil|puree|extract|saffron|masala|turmeric|cinnamon|nutmeg|oregano|chilli|chili|cardamom|fenugreek|lemongrass|tamarind|yeast|cocoa|vanilla|mustard)\b",
                    key,
                ):
                    # skip obvious fresh produce leftovers
                    if re.search(
                        r"(?i)\b(chicken|beef|salmon|carrot|potato|spinach|broccoli|pepper|tomato|mushroom|pasta|rice|oat|bean|tofu)\b",
                        key,
                    ):
                        continue
                    unknown_spicey[key].add(rname)

        items = []
        for key, b in buckets.items():
            items.append(
                {
                    "key": key,
                    "category": b["category"],
                    "count": len(b["recipes"]),
                    "recipes": sorted(b["recipes"]),
                    "labels": sorted(b["labels"]),
                }
            )
        items.sort(key=lambda x: (-x["count"], x["category"], x["key"]))

        # text report
        lines = [
            f"Scanned {len(recipes)} Field Doctor-Style recipes",
            f"Cupboard staples found: {len(items)}",
            "",
        ]
        by_cat: dict[str, list] = defaultdict(list)
        for it in items:
            by_cat[it["category"]].append(it)

        cat_order = [
            "Salt & pepper",
            "Spices",
            "Herbs (dried)",
            "Herbs (fresh / pack)",
            "Thickeners & flours",
            "Condiments & sauces",
            "Oils",
            "Plant milks & liquids",
            "Sweeteners",
            "Baking / bar extras",
            "Cheese (hard)",
        ]
        for cat in cat_order:
            group = by_cat.get(cat) or []
            if not group:
                continue
            lines.append(f"## {cat}")
            for it in sorted(group, key=lambda x: (-x["count"], x["key"])):
                lines.append(f"  {it['count']:2d}x  {it['key']}")
                lines.append(f"      meals: {', '.join(it['recipes'])}")
            lines.append("")

        if unknown_spicey:
            lines.append("## Other spice-like (review / not auto-merged)")
            for k, rs in sorted(unknown_spicey.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                if len(rs) < 1:
                    continue
                lines.append(f"  {len(rs):2d}x  {k}  → {', '.join(sorted(rs))}")

        text = "\n".join(lines)
        OUT_TXT.write_text(text, encoding="utf-8")
        OUT_JSON.write_text(json.dumps({"recipe_count": len(recipes), "items": items}, indent=2), encoding="utf-8")
        print(text)


asyncio.run(main())
