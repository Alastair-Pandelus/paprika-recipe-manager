"""Paprika grocery aisle names + keyword mapping.

Must match live `/v2/sync/groceryaisles/` exactly (name + aisle_uid on POST).
Re-fetch after the user renames/reorders aisles in Paprika, then update
EXPECTED_AISLES / aisle_for() here and the weekly meal-plan skill.
"""
from __future__ import annotations

import re

# Live aisle names as of 2026-09-29 (order_flag ascending). Keep in sync with Paprika.
EXPECTED_AISLES: tuple[str, ...] = (
    "Fruit and Veg",
    "Meat",
    "Seafood",
    "Ready Meals",
    "Dairy",
    "Frozen Foods",
    "Canned and Jar Goods, Pasta and Rice",
    "Oils, Dressings, Spices and Seasonings",
    "Bread",
    "Cereals",
    "Snacks",
    "Beverages",
    "Miscellaneous",
)

# Legacy / script-local names → current Paprika aisle
AISLE_ALIASES: dict[str, str] = {
    "Produce": "Fruit and Veg",
    "Fruit & veg": "Fruit and Veg",
    "Fruit and veg": "Fruit and Veg",
    "Bakery": "Bread",
    "Breads and Cereals": "Cereals",
    "Pasta, Rice and Beans": "Canned and Jar Goods, Pasta and Rice",
    "Baking Goods": "Canned and Jar Goods, Pasta and Rice",
    "Sauces and Condiments": "Oils, Dressings, Spices and Seasonings",
    "Oils and Dressings": "Oils, Dressings, Spices and Seasonings",
    "Spices and Seasonings": "Oils, Dressings, Spices and Seasonings",
    "International Cuisine": "Frozen Foods",
    "Ready meals": "Ready Meals",
    "Ready Meals": "Ready Meals",
    "Meat & fish": "Meat",
    "Dairy & eggs": "Dairy",
    "Dry goods": "Canned and Jar Goods, Pasta and Rice",
    "Frozen": "Frozen Foods",
    "Other / condiments": "Oils, Dressings, Spices and Seasonings",
    "Other": "Miscellaneous",
}


def normalize_aisle(name: str | None) -> str:
    """Map alias or exact name to a live Paprika aisle; else Miscellaneous."""
    if not name:
        return "Miscellaneous"
    if name in EXPECTED_AISLES:
        return name
    if name in AISLE_ALIASES:
        return AISLE_ALIASES[name]
    # case-insensitive exact
    low = name.lower()
    for a in EXPECTED_AISLES:
        if a.lower() == low:
            return a
    return "Miscellaneous"


def aisle_for(ingredient: str, *, is_pack: bool = False) -> str:
    """Heuristic keyword → Paprika grocery aisle (exact live name)."""
    if is_pack or re.search(r"\bwaitrose\b", (ingredient or ""), re.I):
        return "Ready Meals"
    low = (ingredient or "").lower()

    # Choice-of produce lines (often mention "tinned" as an option) → Fruit and Veg first
    if re.search(r"\bany\s+\d+\s+of\s*:", low):
        return "Fruit and Veg"

    # UK: olives are sold in jars
    if re.search(r"\bolives?\b", low) and "oil" not in low:
        return "Canned and Jar Goods, Pasta and Rice"

    # UK: tinned sardines (olive oil / spring water) are canned goods, not fresh seafood
    if re.search(r"\bsardines?\b", low) and re.search(
        r"\b(tin|tinned|canned)\b", low
    ):
        return "Canned and Jar Goods, Pasta and Rice"

    if "frozen" in low or "oven chips" in low or re.search(r"\bpeas\b", low):
        return "Frozen Foods"
    # Dried spices and herbs before the nut/seed check ("nutmeg" contains "nut")
    if any(k in low for k in ("nutmeg", "cinnamon", "mixed herbs", "dried herbs", "italian herbs",
                              "turmeric", "cardamom", "garam masala", "coriander seed", "cumin seed",
                              "fennel seed", "mustard seed", "fenugreek", "dried oregano", "cayenne", "chilli flakes",
                              "chili flakes", "chilli powder", "paprika")) or re.search(r"\b(whole|ground)\s+cloves?\b", low):
        return "Oils, Dressings, Spices and Seasonings"
    # tomato purée, sundried tomatoes and tinned tomatoes are store-cupboard, not fresh veg
    if re.search(r"tomato pur[ée]e|sun-?dried tomato|tinned .*tomato|chopped tomatoes|passata", low):
        return "Canned and Jar Goods, Pasta and Rice"
    # UK: flaked almonds, ground almonds and desiccated coconut sit with the baking goods
    if re.search(r"flaked almonds?|almond flakes?|ground almonds?|coconut flakes?|desiccated coconut", low):
        return "Canned and Jar Goods, Pasta and Rice"
    if any(k in low for k in ("tapioca", "starch", "cornflour", "baking powder", "bicarbonate")):
        return "Canned and Jar Goods, Pasta and Rice"
    if any(k in low for k in ("coffee", "tea")) or re.search(
        r"\b(drink|beverage)s?\b", low
    ):
        return "Beverages"
    if (
        re.search(r"\bcrisps?\b", low)
        or re.search(r"\bbars?\b", low)
        or re.search(r"\bcorn\s*cakes?\b", low)
        or "kallo" in low
    ):
        return "Snacks"
    if any(k in low for k in ("salmon", "fish", "sardine", "seafood", "cod", "haddock")):
        return "Seafood"
    if any(k in low for k in ("chicken", "pork", "prosciutto", "beef", "meat")):
        return "Meat"
    if any(
        k in low
        for k in (
            "milk",
            "cheese",
            "yoghurt",
            "yogurt",
            "ice cream",
            "parmigiano",
            "cheddar",
            "egg",
            "butter",
        )
    ):
        return "Dairy"
    if re.search(r"\boat\s*cakes?\b", low):
        return "Bread"
    if any(
        k in low
        for k in ("sourdough", "bread", "baguette", "bakery", "oat cake")
    ) or re.search(r"\btoast\b", low):  # not "toasted almonds"
        return "Bread"
    if any(k in low for k in ("cereal",)):
        return "Cereals"
    if any(
        k in low
        for k in (
            "rice",
            "pasta",
            "noodle",
            "spaghetti",
            "bean",
            "pesto",
            "jam",
            "sauce",
            "tamari",
            "soy",
            "fish sauce",
            "maple syrup",
            "caster",
            "sugar",
            "flour",
            "tin",
            "tinned",
            "canned",
        )
    ):
        return "Canned and Jar Goods, Pasta and Rice"
    if any(
        k in low
        for k in (
            "olive oil",
            "oil",
            "vinegar",
            "dressing",
            "spice",
            "seasoning",
            "paprika",
            "cumin",
            "ginger",
            "salt",
        )
    ) or re.search(r"\b(black|white|ground)\s+pepper\b", low) or re.search(
        r"\bpepper\b.*\b(to taste|pinch)\b", low
    ):
        # fresh ginger stays Fruit and Veg below if matched earlier — keep spices here
        if "fresh ginger" in low or re.search(r"\bginger,?\s*grated\b", low):
            return "Fruit and Veg"
        if re.search(r"\b(green|red|bell|sweet)\s+peppers?\b", low):
            return "Fruit and Veg"
        return "Oils, Dressings, Spices and Seasonings"
    if any(k in low for k in ("nut", "seed", "snack", "peanut")):
        return "Snacks"
    if any(
        k in low
        for k in (
            "lettuce",
            "salad",
            "spinach",
            "broccoli",
            "carrot",
            "potato",
            "tomato",
            "chive",
            "parsley",
            "coriander",
            "basil",
            "ginger",
            "lime",
            "lemon",
            "kiwi",
            "pineapple",
            "orange",
            "rhubarb",
            "cabbage",
            "radish",
            "bean sprout",
            "courgette",
            "aubergine",
            "eggplant",
            "rocket",
            "banana",
            "berry",
            "strawberr",
            "raspberr",
            "clementine",
            "green bean",
            "fruit",
            "any 3 of",
            "any 1 of",
            "parsnip",
            "mushroom",
            "oyster mushroom",
            "shallot",
            "spring onion",
            "green onion",
            "swede",
            "celeriac",
            "fennel",
            "bell pepper",
            "green pepper",
            "red pepper",
        )
    ):
        return "Fruit and Veg"
    return "Miscellaneous"
