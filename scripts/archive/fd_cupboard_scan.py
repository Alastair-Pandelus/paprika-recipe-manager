"""Extract cupboard/pantry staples reused across Field Doctor-Style recipes."""
from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from pathlib import Path

import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
OUT = Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_cupboard_staples.txt")

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Patterns that look like cupboard staples (spices/herbs/flours/condiments/etc.)
STAPLE_HINTS = re.compile(
    r"""(?ix)
    \b(
      pepper|salt|paprika|cumin|coriander|turmeric|cinnamon|ginger|
      chilli|chili|chipotle|cayenne|oregano|thyme|basil|rosemary|
      mixed\s*herbs|herbes?|bay|fennel|mustard|garam|masala|
      curry|saffron|sumac|zaatar|allspice|nutmeg|clove|cardamom|
      tapioca|cornflour|cornstarch|flour|starch|
      stock|bouillon|soy\s*sauce|tamari|fish\s*sauce|worcester|
      vinegar|mirin|sesame|oil|olive|ghee|
      sugar|syrup|honey|maple|
      tomato\s*puree|tomato\s*paste|passata|
      coconut\s*milk|oat\s*milk|soy\s*milk|almond|
      baking|yeast|vanilla|cocoa|cacao|
      rice\s*vinegar|lime\s*leaves?|lemongrass|
      garlic.infused|onion.infused|asafoetida|hing|
      nutritional\s*yeast|miso|tahini|pesto
    )\b
    """
)

# Fresh produce / proteins / bulk veg — exclude from cupboard list even if matched
EXCLUDE = re.compile(
    r"""(?ix)
    \b(
      chicken|beef|lamb|pork|salmon|hake|fish|tofu|edamame|
      carrot|potato|sweet\s*potato|spinach|broccoli|courgette|
      aubergine|pepper(?!\s)|green\s*pepper|red\s*pepper|
      tomato(?!\s*puree|\s*paste)|plum\s*tomato|chopped\s*tomato|
      mushroom|oyster|kale|swede|celeriac|celery|fennel(?!\s*seed)|
      beansprout|green\s*bean|chickpea|kidney|black\s*bean|
      quinoa|rice(?!\s*syrup|\s*flour|\s*noodle|\s*protein)|
      pasta|penne|noodle|oat(?!\s*milk|\s*flour)|rolled\s*oat|jumbo|
      peanut(?!\s*butter)|almond(?!\s*milk)|hazelnut|walnut|
      banana|cherry|chocolate\s*chip|cocoa\s*nibs?
    )\b
    """
)

# Force-include common spice/cupboard names even without hint match after strip
FORCE_CUPBOARD = {
    "black pepper",
    "white pepper",
    "salt",
    "sea salt",
    "tapioca flour",
    "tapioca starch",
    "cornflour",
    "corn flour",
    "extra virgin olive oil",
    "olive oil",
    "garlic infused oil",
    "onion infused oil",
    "tomato puree",
    "tomato paste",
    "brown sugar",
    "caster sugar",
    "soy sauce",
    "tamari",
    "fish sauce",
    "mixed herbs",
    "herbes de provence",
    "smoked paprika",
    "sweet paprika",
    "sweet noble paprika",
    "ground cumin",
    "ground coriander",
    "ground turmeric",
    "ground cinnamon",
    "ground ginger",
    "dried oregano",
    "dried thyme",
    "bay leaves",
    "bay leaf",
    "chives",
    "parsley",
    "coriander leaf",
    "fresh coriander",
    "chilli flakes",
    "chili flakes",
    "red chilli flakes",
    "mustard powder",
    "dijon mustard",
    "wholegrain mustard",
    "garam masala",
    "curry powder",
    "lemongrass",
    "kaffir lime leaves",
    "lime leaves",
    "sesame oil",
    "sesame seeds",
    "rice vinegar",
    "cider vinegar",
    "white wine vinegar",
    "red wine vinegar",
    "balsamic vinegar",
    "coconut milk",
    "oat milk",
    "soy milk",
    "almond milk",
    "organic brown rice syrup",
    "brown rice syrup",
    "cocoa powder",
    "vanilla extract",
    "baking powder",
    "nutritional yeast",
    "parmigiano",
    "parmesan",
    "italian hard cheese",
}


def clean_ingredient_name(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    # strip quantity prefix: "160 g Foo", "2 tsp Bar", "1/2 x 400 g tins Baz"
    line = re.sub(
        r"""(?ix)^
        (
          \d+\s*/\s*\d+|\d+(?:\.\d+)?|
          \d+\s+\d+/\d+
        )
        \s*
        (
          x\s*)?
        (
          (?:medium|large|small|heaped|level)\s+
        )?
        (
          g|kg|ml|l|tsp|tbsp|teaspoon|tablespoon|cup|cups|
          tin|tins|clove|cloves|pack|packs|pinch|pinches|
          handful|handfuls|slice|slices|sprig|sprigs|
          leaf|leaves|stick|sticks|bunch|bunches
        )?
        \s*
        """,
        "",
        line,
    )
    # leftover "x 400 g tins "
    line = re.sub(r"(?ix)^x\s*\d+\s*g\s*(?:tin|tins)?\s*", "", line)
    line = re.sub(r"(?ix)^(tin|tins)\s+", "", line)
    line = re.sub(r"(?ix)^(medium|large|small)\s+", "", line)
    # strip bracketed allergen/composition noise for grouping, keep short tags
    name = re.sub(r"\s*\[[^\]]*\]\s*", " ", line)
    name = re.sub(r"\s+", " ", name).strip(" -")
    return name


def normalize_key(name: str) -> str:
    n = name.lower()
    n = n.replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9\s/+-]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # aliases
    aliases = {
        "black pepper": "black pepper",
        "pepper": "black pepper",
        "ground black pepper": "black pepper",
        "sweet noble paprika": "smoked/sweet paprika",
        "sweet paprika": "smoked/sweet paprika",
        "smoked paprika": "smoked/sweet paprika",
        "paprika": "smoked/sweet paprika",
        "mixed herbs": "mixed herbs",
        "herb mix": "mixed herbs",
        "herbes de provence": "mixed herbs",
        "tomato puree": "tomato puree",
        "tomato paste": "tomato puree",
        "tapioca flour": "tapioca flour",
        "tapioca starch": "tapioca flour",
        "cornflour": "cornflour",
        "corn starch": "cornflour",
        "cornstarch": "cornflour",
        "extra virgin olive oil": "extra virgin olive oil",
        "olive oil": "extra virgin olive oil",
        "evoo": "extra virgin olive oil",
        "soy sauce": "soy sauce / tamari",
        "tamari": "soy sauce / tamari",
        "gluten free soy sauce": "soy sauce / tamari",
        "chives": "chives",
        "chive": "chives",
        "chive rings": "chives",
        "parmigiano": "parmesan / parmigiano",
        "parmesan": "parmesan / parmigiano",
        "italian hard cheese": "parmesan / parmigiano",
        "garlic infused oil": "garlic-infused oil",
        "garlic-infused oil": "garlic-infused oil",
        "onion infused oil": "onion-infused oil",
        "onion-infused oil": "onion-infused oil",
        "bay leaf": "bay leaves",
        "bay leaves": "bay leaves",
        "ground cumin": "ground cumin",
        "cumin": "ground cumin",
        "ground coriander": "ground coriander",
        "coriander": "ground coriander",  # may also be leaf — handled below
        "ground turmeric": "ground turmeric",
        "turmeric": "ground turmeric",
        "ground cinnamon": "ground cinnamon",
        "cinnamon": "ground cinnamon",
        "ground ginger": "ground ginger",
        "ginger": "ground ginger",
        "dried oregano": "dried oregano",
        "oregano": "dried oregano",
        "dried thyme": "dried thyme",
        "thyme": "dried thyme",
        "chilli flakes": "chilli flakes",
        "chili flakes": "chilli flakes",
        "red chilli": "chilli flakes",
        "mustard powder": "mustard powder",
        "english mustard powder": "mustard powder",
        "brown sugar": "brown sugar",
        "organic brown rice syrup": "brown rice syrup",
        "brown rice syrup": "brown rice syrup",
        "soy milk": "soy milk",
        "oat milk": "oat milk",
        "coconut milk": "coconut milk",
        "almond milk": "almond milk",
        "organic almond dairy free milk": "almond milk",
        "sesame oil": "sesame oil",
        "sesame seed oil": "sesame oil",
        "fish sauce": "fish sauce",
        "rice vinegar": "rice vinegar",
        "cider vinegar": "cider vinegar",
        "white wine vinegar": "white wine vinegar",
        "red wine vinegar": "red wine vinegar",
        "lemongrass": "lemongrass",
        "kaffir lime leaf": "lime leaves",
        "kaffir lime leaves": "lime leaves",
        "lime leaves": "lime leaves",
        "garam masala": "garam masala",
        "curry powder": "curry powder",
        "cocoa powder": "cocoa powder",
        "nutritional yeast": "nutritional yeast",
        "baking powder": "baking powder",
        "vanilla extract": "vanilla extract",
        "maple syrup": "maple syrup",
        "honey": "honey",
        "asafoetida": "asafoetida",
        "hing": "asafoetida",
    }
    if n in aliases:
        return aliases[n]
    # fresh coriander leaf vs ground
    if "coriander" in n and ("leaf" in n or "fresh" in n or "leaves" in n):
        return "fresh coriander / cilantro"
    if n.startswith("coriander ") and "ground" not in n and "seed" not in n:
        # ambiguous — keep as-is unless spice-like
        pass
    return aliases.get(n, n)


def looks_cupboard(name: str, key: str) -> bool:
    low = name.lower()
    if key in FORCE_CUPBOARD or low in FORCE_CUPBOARD:
        return True
    if STAPLE_HINTS.search(low) or STAPLE_HINTS.search(key):
        # still exclude clear fresh bulk if wrongly matched
        if re.search(
            r"(?i)\b(chicken|beef|salmon|carrot|potato|spinach|broccoli|courgette|aubergine|mushroom|pasta|penne|oyster)\b",
            low,
        ):
            return False
        return True
    # dried / ground / powder / flakes / infused oil
    if re.search(r"(?i)\b(ground|dried|powder|flakes|infused|extract|syrup|vinegar|stock|bouillon)\b", low):
        return True
    return False


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
            name = recipe.get("name") or ""
            if recipe.get("in_trash"):
                continue
            if "Field Doctor-Style" not in name:
                continue
            recipes.append(recipe)

        # key -> {display, recipes:set, raw_names:set}
        staples: dict[str, dict] = {}
        all_freq: dict[str, set] = defaultdict(set)

        for recipe in recipes:
            rname = recipe["name"]
            seen_keys = set()
            for line in (recipe.get("ingredients") or "").splitlines():
                raw = clean_ingredient_name(line)
                if not raw:
                    continue
                key = normalize_key(raw)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                all_freq[key].add(rname)
                if looks_cupboard(raw, key):
                    bucket = staples.setdefault(
                        key, {"display": raw, "recipes": set(), "names": set()}
                    )
                    bucket["recipes"].add(rname)
                    bucket["names"].add(raw)

        # sort by recipe count desc
        ranked = sorted(staples.items(), key=lambda kv: (-len(kv[1]["recipes"]), kv[0]))

        lines = []
        lines.append(f"Field Doctor-Style recipes scanned: {len(recipes)}")
        lines.append(f"Cupboard/pantry candidates: {len(ranked)}")
        lines.append("")
        lines.append("=== CUPBOARD STAPLES (by # of recipes) ===")
        lines.append("")
        for key, info in ranked:
            n = len(info["recipes"])
            variants = ", ".join(sorted(info["names"]))
            meal_list = "; ".join(
                sorted(
                    re.sub(r"^Field Doctor-Style\s+", "", r).replace(" (Low FODMAP)", "")
                    for r in info["recipes"]
                )
            )
            lines.append(f"{n:2d}x  {key}")
            if variants.lower() != key:
                lines.append(f"     as labelled: {variants}")
            lines.append(f"     in: {meal_list}")
            lines.append("")

        # Also list high-frequency non-cupboard for reference? skip — user asked cupboard
        text = "\n".join(lines)
        OUT.write_text(text, encoding="utf-8")
        print(text)


asyncio.run(main())
