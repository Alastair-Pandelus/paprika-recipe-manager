"""Bay's Kitchen stir-in sauces, reverse engineered as home recipes (one 260 g jar, 2 x 130 g servings).

Amounts are inferred from the published UK ingredient order (descending weight) and the
percentages given, then checked against the label's fat, saturates, sugars and salt per
100 g. The home versions keep the flavour but use single raw spices, little oil (1 tsp a
jar, only where spices need blooming) and light coconut milk instead of coconut cream.

  python scripts/tools/bays_kitchen_sauces.py            # dry run: scores + drafts file
  python scripts/tools/bays_kitchen_sauces.py --apply    # create the recipes in Paprika
"""
from __future__ import annotations

import asyncio
import datetime
import gzip
import hashlib
import io
import json
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from cleanup_library import calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402

DATA = ROOT / "data" / "bays_kitchen" / "products.json"
DRAFTS = ROOT / "docs" / "bays-kitchen-drafts.md"
OIL = "1 tsp olive oil"
LCM = "light coconut milk"

SPICE_NOTE = (
    "Spices: whole seeds (coriander, cumin, fennel, fenugreek, mustard) are dry-toasted with no oil, then ground. "
    "Fat-soluble spices (turmeric, paprika, chilli, cayenne, ground cumin and coriander) are bloomed briefly in the 1 tsp "
    "of oil, with a splash of water if they catch. Water-soluble aromatics (cinnamon/cassia, cardamom, cloves, lime leaf, "
    "dried herbs) simply simmer in the sauce."
)

# handle -> recipe; amounts per jar (260 g). 'label' is our reverse-engineered factory recipe (g), for the notes.
SAUCES = {
    "tomato-basil-stir-in-sauce": dict(
        name="Bay's Kitchen Tomato & Basil Sauce",
        label="Tomatoes 138 g (53%), water 64 g, tomato purée 31 g (12%), olive oil 6 g, balsamic vinegar 6 g, tapioca starch 5 g, basil 3 g (1.2%), chives 2.5 g, dextrose 2 g, salt 1.2 g, oregano 0.5 g, lactic acid, black pepper",
        ingredients=[
            "140 g tinned chopped tomatoes",
            "65 ml water",
            "2 tbsp tomato purée",
            "1 tsp balsamic vinegar",
            "1 tsp olive oil",
            "1 tbsp tapioca starch (optional, mixed with 1 tbsp of the water)",
            "3 g fresh basil leaves (about 10 leaves), torn",
            "1 tbsp chopped chives",
            "1/2 tsp caster sugar",
            "1/4 tsp salt",
            "1/2 tsp dried oregano",
            "1/2 tsp lemon juice",
            "1 pinch ground black pepper",
        ],
        directions=[
            "Warm the olive oil in a small pan over a medium-low heat (oregano and black pepper don't need frying, so no extra oil).",
            "Add the tomatoes, tomato purée, water, balsamic vinegar, oregano, sugar and salt. Simmer for 15-20 minutes, stirring now and then, until slightly thickened.",
            "For a thicker sauce, stir in the tapioca starch mixed with water and simmer for 1 minute more (or simmer 10 minutes longer instead).",
            "Off the heat, stir in the chives, basil, lemon juice and black pepper. Taste for salt.",
        ],
        changes="1 tsp olive oil instead of about 1 1/2 tsp; sugar instead of dextrose; lemon juice instead of lactic acid.",
    ),
    "spicy-arrabbiata-stir-in-sauce": dict(
        name="Bay's Kitchen Spicy Arrabbiata Sauce",
        label="Tomatoes 187 g (72%), water 37 g, tomato purée 18 g (7%), sundried tomatoes 5 g (1.8%), olive oil 5 g, tapioca starch 4 g, dextrose 2 g, salt 1.5 g, lemon juice 1 g, cayenne 0.5 g, dried chillies 0.3 g (0.1%), black pepper",
        ingredients=[
            "190 g tinned chopped tomatoes",
            "40 ml water",
            "1 tbsp tomato purée",
            "5 g sundried tomatoes (dry-packed, about 1 piece), finely chopped",
            "1 tsp olive oil",
            "1 tbsp tapioca starch (optional, mixed with 1 tbsp of the water)",
            "1/2 tsp caster sugar",
            "1/4 tsp salt",
            "1/4 tsp lemon juice",
            "1/4 tsp cayenne pepper",
            "1 pinch dried chilli flakes",
            "1 pinch ground black pepper",
        ],
        directions=[
            "Warm the olive oil over a medium-low heat, add the cayenne and chilli flakes and stir for 20-30 seconds (chilli heat is fat-soluble, so this is the step that needs the oil).",
            "Add the tomatoes, tomato purée, sundried tomatoes, water, sugar and salt. Simmer for 15-20 minutes until slightly thickened.",
            "For a thicker sauce, stir in the tapioca starch mixed with water and simmer for 1 minute more.",
            "Finish with the lemon juice and black pepper.",
        ],
        changes="Sugar instead of dextrose; oil kept to 1 tsp.",
    ),
    "tikka-masala-stir-in-sauce": dict(
        name="Bay's Kitchen Tikka Masala Sauce",
        label="Tomatoes 95 g, water 81 g, coconut cream 29 g (11%), tomato purée 16 g, olive oil 11 g, tapioca starch 6 g, coriander leaf 4 g, cumin 3 g, dextrose 2.5 g, lemon juice 2.4 g, coriander seed 2.4 g, ginger 2.3 g, salt 2.3 g, paprika 1.5 g, turmeric 1 g, dried chillies 0.3 g, black pepper",
        ingredients=[
            "95 g tinned chopped tomatoes",
            "80 ml water",
            f"30 g {LCM}",
            "1 tbsp tomato purée",
            OIL,
            "4 tsp tapioca starch, mixed with 1 tbsp of the water",
            "4 g fresh coriander (a small handful), chopped",
            "1 1/2 tsp cumin seeds",
            "1/2 tsp caster sugar",
            "2 tsp lemon juice",
            "1 1/2 tsp coriander seeds",
            "2 g fresh ginger (about 1 tsp grated)",
            "1/3 tsp salt",
            "2/3 tsp sweet paprika",
            "1/3 tsp ground turmeric",
            "1 pinch dried chilli flakes",
            "1 pinch ground black pepper",
        ],
        directions=[
            "Dry-toast the cumin and coriander seeds in a dry pan for 1-2 minutes until fragrant, then grind.",
            "Warm the olive oil over a medium-low heat. Add the ginger for 30 seconds, then the ground seeds, paprika, turmeric and chilli and stir for 30-45 seconds, adding a splash of the water if they catch.",
            "Add the tomatoes, tomato purée, the rest of the water, sugar and salt. Simmer for 15 minutes.",
            "Stir in the light coconut milk and the tapioca starch mixed with water; simmer for 1-2 minutes until thickened.",
            "Finish with the lemon juice, fresh coriander and black pepper.",
        ],
        changes="Light coconut milk instead of coconut cream, and 1 tsp olive oil instead of about 2 1/2 tsp (saturated fat about 3.6 g to about 1 g a serving); whole cumin and coriander seeds toasted and ground.",
    ),
    "mild-korma-stir-in-sauce": dict(
        name="Bay's Kitchen Korma Curry Sauce",
        label="Coconut cream 88 g (34%), water 85 g, tomatoes 30 g, ginger 15 g, dextrose 10 g, olive oil 10 g, tapioca starch 7 g, turmeric 4 g, garam masala 3.4 g (1.3%; coriander seed, cumin, ginger, cassia, black pepper, cloves), salt 3.3 g, lemon juice 2 g, cumin 1 g, cardamom 0.5 g",
        ingredients=[
            f"90 g {LCM}",
            "85 ml water",
            "30 g tinned chopped tomatoes",
            "15 g fresh ginger (about 2 1/2 tbsp grated)",
            "2 1/2 tsp caster sugar",
            OIL,
            "2 tbsp tapioca starch, mixed with 2 tbsp of the water",
            "1 1/4 tsp ground turmeric",
            "1 tsp coriander seeds",
            "1/2 tsp cumin seeds",
            "1/4 tsp ground cinnamon (or cassia)",
            "2 whole cloves",
            "1 pinch ground black pepper",
            "1/2 tsp salt",
            "1/2 tsp lemon juice",
            "4 green cardamom pods, lightly crushed",
        ],
        directions=[
            "Dry-toast the coriander and cumin seeds for 1-2 minutes until fragrant, then grind (this replaces the garam masala blend).",
            "Warm the olive oil over a medium-low heat. Add the ginger for 1 minute, then the ground seeds and turmeric for 30 seconds, adding a splash of water if they catch.",
            "Add the tomatoes, water, sugar, salt, cinnamon, cloves and cardamom (these infuse in the liquid, no frying needed). Simmer for 10 minutes, then fish out the cloves and cardamom pods.",
            "Stir in the light coconut milk and the tapioca starch mixed with water; simmer gently for 2 minutes until creamy. Do not boil hard.",
            "Finish with the lemon juice and black pepper.",
        ],
        changes="Light coconut milk instead of coconut cream (saturated fat about 10 g to about 3 g a serving); 1 tsp olive oil instead of about 2 tsp; a little more tapioca starch for body; garam masala made from single spices; sugar instead of dextrose.",
    ),
    "katsu-curry-stir-in-sauce": dict(
        name="Bay's Kitchen Katsu Curry Sauce",
        label="Water 112 g, coconut cream 81 g (31%), wheat-free soy sauce 21 g (8%), dextrose 12 g, tapioca starch 10 g, curry powder 7 g (2.7%; coriander, turmeric, fenugreek, chilli, salt, cumin, fennel, mustard, black pepper), ginger 7 g, olive oil 6 g, lemon juice 3 g, turmeric 1 g",
        ingredients=[
            "110 ml water",
            f"80 g {LCM}",
            "1 1/2 tbsp tamari or gluten-free soy sauce",
            "1 tbsp caster sugar",
            "2 tbsp tapioca starch, mixed with 2 tbsp of the water",
            "1 tsp coriander seeds",
            "3/4 tsp ground turmeric",
            "1/4 tsp fenugreek seeds",
            "1/4 tsp chilli powder (mild)",
            "1/3 tsp cumin seeds",
            "1/4 tsp fennel seeds",
            "1/8 tsp mustard seeds",
            "1 pinch ground black pepper",
            "7 g fresh ginger (about 1 tbsp grated)",
            OIL,
            "2 tsp lemon juice",
        ],
        directions=[
            "Dry-toast the coriander, fenugreek, cumin, fennel and mustard seeds for 1-2 minutes until fragrant (fenugreek turns bitter if it goes past golden), then grind. This replaces the curry powder.",
            "Warm the olive oil over a medium-low heat. Add the ginger for 30 seconds, then the ground seeds, turmeric and chilli powder for 30 seconds, adding a splash of water if they catch.",
            "Add the water, tamari and sugar and simmer for 5 minutes.",
            "Stir in the light coconut milk and the tapioca starch mixed with water; simmer for 2 minutes until thick and glossy. For the classic smooth katsu sauce, blend.",
            "Finish with the lemon juice and black pepper.",
        ],
        changes="Light coconut milk instead of coconut cream (saturated fat about 9 g to about 2.5 g a serving); 1 tsp olive oil; curry powder made from single spices; sugar instead of dextrose; tamari for the wheat-free soy sauce.",
    ),
    "sweet-sour-stir-in-sauce": dict(
        name="Bay's Kitchen Sweet & Sour Sauce",
        label="Water 129 g, sugar 27 g, pineapple juice concentrate 18 g (7%), pineapple 16 g (6%), tomato purée 16 g, cider vinegar 16 g (6%), green peppers 14 g, red peppers 12 g, tapioca starch 10 g, olive oil 1.5 g, chives 1 g, salt 0.7 g, black pepper",
        ingredients=[
            "75 ml water",
            "75 ml unsweetened pineapple juice",
            "2 tbsp caster sugar",
            "15 g pineapple (fresh or tinned in juice), finely diced",
            "1 tbsp tomato purée",
            "1 tbsp cider vinegar",
            "15 g green pepper, finely diced",
            "12 g red pepper, finely diced",
            "2 tbsp tapioca starch, mixed with 2 tbsp of the water",
            "1 tsp chopped chives",
            "1 pinch salt",
            "1 pinch ground black pepper",
        ],
        directions=[
            "Put the water, pineapple juice, sugar, tomato purée, cider vinegar and salt in a small pan and bring to a simmer. No oil is needed: there are no spices to bloom.",
            "Add the peppers and pineapple and simmer for 3-4 minutes until just tender.",
            "Stir in the tapioca starch mixed with water and simmer for 1 minute until thick and glossy.",
            "Finish with the chives and black pepper.",
        ],
        changes="Unsweetened pineapple juice instead of concentrate (about 4 parts juice per part concentrate, with less water); no oil.",
    ),
    "thai-massaman-stir-in-sauce": dict(
        name="Bay's Kitchen Thai Massaman Sauce",
        label="Water 130 g, coconut cream 91 g (35%), lemongrass 8 g (3%), ginger 7 g, tapioca starch 6 g, coriander leaf 4 g, spices 3.6 g (1.4%; coriander seed, cumin, cayenne, cinnamon, cardamom, turmeric), olive oil 3.5 g, salt 2.8 g, lime juice 2.2 g, dextrose 1.5 g, lime leaf 0.8 g (0.3%)",
        ingredients=[
            "130 ml water",
            f"90 g {LCM}",
            "8 g lemongrass (about 1 stalk, tender inner part), finely chopped",
            "7 g fresh ginger (about 1 tbsp grated)",
            "4 tsp tapioca starch, mixed with 1 tbsp of the water",
            "4 g fresh coriander, stalks and leaves",
            "2/3 tsp coriander seeds",
            "1/3 tsp cumin seeds",
            "1 pinch cayenne pepper",
            "1/4 tsp ground cinnamon",
            "3 green cardamom pods, lightly crushed",
            "1/8 tsp ground turmeric",
            OIL,
            "1/2 tsp salt",
            "1/2 tsp lime juice",
            "1/3 tsp caster sugar",
            "2 kaffir lime leaves",
        ],
        directions=[
            "Dry-toast the coriander and cumin seeds for 1-2 minutes, then grind.",
            "Blitz or pound the lemongrass, ginger and coriander stalks with 1 tbsp of the water to a paste (no oil).",
            "Warm the olive oil over a medium-low heat and fry the paste for 2 minutes, then add the ground seeds, cayenne and turmeric for 30 seconds.",
            "Add the rest of the water, cinnamon, cardamom, lime leaves, salt and sugar (these infuse in the liquid). Simmer for 10 minutes; remove the cardamom pods and lime leaves.",
            "Stir in the light coconut milk and the tapioca starch mixed with water; simmer gently for 2 minutes. Finish with the lime juice and coriander leaves.",
        ],
        changes="Light coconut milk instead of coconut cream (saturated fat about 9.8 g to about 2.8 g a serving); 1 tsp olive oil; massaman spice mix made from single spices; sugar instead of dextrose.",
    ),
    "thai-green-curry-stir-in-sauce": dict(
        name="Bay's Kitchen Thai Green Curry Sauce",
        label="Coconut cream 91 g (35%), water 42 g, bamboo shoots 35 g, green peppers 28 g, red peppers 22 g, coriander 8 g, lemongrass 4.4 g (1.7%), green chillies 4.4 g (1.7%), ginger 4.4 g, wheat-free soy sauce 4.4 g, olive oil 4.3 g, tapioca starch 4 g, lime juice 3 g, sugar 2 g, salt 1.7 g, cumin 0.5 g, lime leaf 0.4 g",
        ingredients=[
            f"90 g {LCM}",
            "40 ml water",
            "35 g bamboo shoots (tinned, drained)",
            "28 g green pepper, sliced",
            "22 g red pepper, sliced",
            "8 g fresh coriander, stalks and leaves",
            "4 g lemongrass (about 1/2 stalk, tender inner part), finely chopped",
            "4 g green chilli (about 1), deseeded for less heat",
            "4 g fresh ginger (about 2 tsp grated)",
            "1 tsp tamari or gluten-free soy sauce",
            OIL,
            "1 tbsp tapioca starch, mixed with 1 tbsp of the water",
            "1/2 tbsp lime juice",
            "1/2 tsp caster sugar",
            "1/4 tsp salt",
            "1/4 tsp cumin seeds",
            "1 kaffir lime leaf",
        ],
        directions=[
            "Dry-toast the cumin seeds for 1 minute, then grind.",
            "Blitz or pound the coriander stalks, lemongrass, green chilli and ginger with 1 tbsp of the water to a green paste (no oil).",
            "Warm the olive oil over a medium-low heat and fry the paste with the cumin for 2 minutes until fragrant.",
            "Add the rest of the water, the light coconut milk, lime leaf, tamari, sugar and salt and bring to a gentle simmer.",
            "Add the bamboo shoots and peppers and simmer for 4-5 minutes. Stir in the tapioca starch mixed with water and simmer for 1 minute.",
            "Finish with the lime juice and coriander leaves; remove the lime leaf.",
        ],
        changes="Light coconut milk instead of coconut cream (saturated fat about 10 g to about 2.8 g a serving); 1 tsp olive oil; tamari for the wheat-free soy sauce.",
    ),
    "thai-red-curry-stir-in-sauce": dict(
        name="Bay's Kitchen Thai Red Curry Sauce",
        label="Coconut cream 94 g (36%), water 61 g, red peppers 50 g, red chillies 9 g (3.5%), lemongrass 7 g (2.6%), tapioca starch 6.5 g, ginger 6.3 g, wheat-free soy sauce 6.2 g, olive oil 6 g, salt 3.3 g, cumin 2.5 g, lime juice 2.3 g, coriander 2 g, dextrose 1.9 g, lime leaf 1.8 g (0.7%), cayenne 0.5 g",
        ingredients=[
            f"95 g {LCM}",
            "60 ml water",
            "50 g red pepper, sliced",
            "9 g red chillies (about 2), deseeded for less heat",
            "7 g lemongrass (about 1 stalk, tender inner part), finely chopped",
            "4 tsp tapioca starch, mixed with 1 tbsp of the water",
            "6 g fresh ginger (about 1 tbsp grated)",
            "1 tsp tamari or gluten-free soy sauce",
            OIL,
            "1/2 tsp salt",
            "1 tsp cumin seeds",
            "1/2 tsp lime juice",
            "2 g fresh coriander, chopped",
            "1/2 tsp caster sugar",
            "3 kaffir lime leaves",
            "1/4 tsp cayenne pepper",
        ],
        directions=[
            "Dry-toast the cumin seeds for 1 minute, then grind.",
            "Blitz or pound the red chillies, lemongrass and ginger with 1 tbsp of the water to a red paste (no oil).",
            "Warm the olive oil over a medium-low heat and fry the paste with the cumin and cayenne for 2 minutes until fragrant.",
            "Add the rest of the water, the light coconut milk, lime leaves, tamari, sugar and salt and bring to a gentle simmer.",
            "Add the red pepper and simmer for 4-5 minutes. Stir in the tapioca starch mixed with water and simmer for 1 minute.",
            "Finish with the lime juice and coriander; remove the lime leaves.",
        ],
        changes="Light coconut milk instead of coconut cream (saturated fat about 10 g to about 2.9 g a serving); 1 tsp olive oil; sugar instead of dextrose; tamari for the wheat-free soy sauce.",
    ),
    "teriyaki-stir-in-sauce": dict(
        name="Bay's Kitchen Teriyaki Sauce",
        label="Wheat-free soy sauce 73 g (28%), water 72 g, sugar 48 g, rice vinegar 20 g, sake 18 g (7%), ginger 18 g (7%), chives 5 g, tapioca starch 4 g, acetic acid",
        ingredients=[
            "5 tbsp tamari or gluten-free soy sauce",
            "90 ml water",
            "2 tbsp maple syrup",
            "1 1/2 tbsp rice vinegar",
            "18 g fresh ginger (about 3 tbsp grated)",
            "1 tbsp chopped chives",
            "1 tbsp tapioca starch, mixed with 1 tbsp of the water",
        ],
        directions=[
            "Put the tamari, water, maple syrup, rice vinegar and ginger in a small pan and simmer for 4-5 minutes until slightly reduced. No oil is needed.",
            "Stir in the tapioca starch mixed with water and simmer for 1 minute until glossy.",
            "Stir in the chives. Use as a glaze or stir-in sauce.",
        ],
        changes="Zero alcohol: the 18 g of sake is replaced with extra water (and the rice vinegar keeps the tang); tamari for the wheat-free soy sauce; 2 tbsp maple syrup instead of 4 tbsp caster sugar, to cut the sugar (about 25 g to about 13 g a serving).",
    ),
}


# Finished weight of one portion (half the batch): ingredient weights less simmer losses.
PORTION = {"tomato-basil-stir-in-sauce": 125, "spicy-arrabbiata-stir-in-sauce": 120, "tikka-masala-stir-in-sauce": 125,
           "mild-korma-stir-in-sauce": 130, "katsu-curry-stir-in-sauce": 135, "sweet-sour-stir-in-sauce": 135,
           "thai-massaman-stir-in-sauce": 125, "thai-green-curry-stir-in-sauce": 130, "thai-red-curry-stir-in-sauce": 130,
           "teriyaki-stir-in-sauce": 135}


def build(handle: str, product: dict, recipe: dict) -> dict:
    lines = "\n".join(recipe["ingredients"])
    notes = "\n".join([
        f"Home version of Bay's Kitchen {product['title']} ({product['url']}), certified low FODMAP by the maker. One 260 g jar = 2 x 130 g servings.",
        f"Label ingredients: {product['ingredients'].replace(' | ', '').replace('For allergens, see ingredients inbold.', '').strip()}",
        f"Label nutrition per 100 g: {product['nutrition'].replace(' | ', ', ')}",
        f"Reverse-engineered factory amounts per jar (UK descending order, given percentages, checked against the label): {recipe['label']}.",
        f"Changed (home version): {recipe['changes']}",
        SPICE_NOTE,
        f"Portion: one serving is about {PORTION[handle]} g of finished sauce, enough for one serving of most of the Bay's Kitchen meals.",
    ])
    freeze = f"Split into {PORTION[handle]} g single portions, then freeze."
    directions = "\n\n".join(f"{i}. {d}" for i, d in enumerate(recipe["directions"] + [freeze], 1))
    t = f.transform_recipe(recipe["name"], lines, "", notes, servings="2")
    t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings="2")
    return dict(name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], notes=notes,
                directions=directions, servings="2", source="Bay's Kitchen", source_url=product["url"])


async def main() -> None:
    apply = "--apply" in sys.argv
    products = json.loads(DATA.read_text(encoding="utf-8"))
    built = {h: build(h, products[h], r) for h, r in SAUCES.items()}
    md = ["# Bay's Kitchen sauces: draft home recipes", ""]
    for h, b in built.items():
        md += [f"## {b['name']}", "", b["ingredients"], "", b["directions"], "", b["notes"], ""]
        print(f"{b['name']:55} | {b['description'].splitlines()[-1] if b['description'] else ''}")
    DRAFTS.write_text("\n".join(md), encoding="utf-8")
    print(f"drafts -> {DRAFTS}")
    if not apply:
        return
    raise SystemExit("apply step is in create_bays_kitchen.py")


if __name__ == "__main__":
    asyncio.run(main())
