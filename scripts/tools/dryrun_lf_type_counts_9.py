"""Dry-run counts for 9 LF type categories (no Paprika writes)."""
from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

TYPES = [
    "Main Meals",
    "Soups",
    "Sides & Salads",
    "Breakfast",
    "Baking & Sweet Treats",
    "Snacks & Bars",
    "Smoothies and Drinks",
    "Sauces",
    "Ice Cream and Frozen Desserts",
]

# Fodmap Tracker name prefixes -> type
FT_PREFIX = {
    "meal": "Main Meals",
    "soup": "Soups",
    "side": "Sides & Salads",
    "breakfast": "Breakfast",
    "sweet": "Baking & Sweet Treats",
    "sauce": "Sauces",
    "smoothie": "Smoothies and Drinks",
    "mocktail": "Smoothies and Drinks",
    "snack": "Snacks & Bars",
}

# Ordered rules: first match wins (more specific first)
RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "Ice Cream and Frozen Desserts",
        re.compile(
            r"\b(ice\s*cream|gelato|sorbet|frozen\s*yogurt|popsicle|ice\s*lolly|"
            r"magnum|nice\s*cream|frozen\s*dessert)\b",
            re.I,
        ),
    ),
    (
        "Smoothies and Drinks",
        re.compile(
            r"\b(smoothie|mocktail|latte|lemonade|iced\s*(coffee|matcha)|"
            r"hot\s*chocolate|turmeric\s*latte|matcha\s*soda|ginger\s*tea|"
            r"eggnog|margarita|mojito|sangria|pina\s*colada|cider|"
            r"(?<!butter )tea\b|(?<!olive )juice\b)\b",
            re.I,
        ),
    ),
    (
        "Snacks & Bars",
        re.compile(
            r"\b(granola\s*bars?|protein\s*bars?|energy\s*bars?|energy\s*balls?|"
            r"snack\s*mix|trail\s*mix|popcorn|kale\s*chips|nachos|"
            r"cheese\s*ball|deviled\s*eggs|jalape[nñ]o\s*poppers|"
            r"potato\s*skins|spiced\s*nuts|roasted\s*chickpeas|"
            r"chocolate\s*bark|toffee\s*bark|yogurt\s*bark|"
            r"buffalo\s*chicken\s*dip|spinach\s*dip|layer(?:ed)?\s*.*dip|"
            r"no[\s-]?bake\s*bars?)\b",
            re.I,
        ),
    ),
    (
        "Sauces",
        re.compile(
            r"^(?=.*\b(sauce|dressing|vinaigrette|pesto|marinade|mayo|"
            r"mayonnaise|gravy|seasoning|chimichurri|ketchup|aioli|"
            r"hummus|tzatziki|ranch|salsa(?!\s+(chicken|verde\s+chicken))|"
            r"condiment|pickles?)\b)"
            r"(?!.*\b(chicken|beef|pork|salmon|shrimp|tofu|pasta|taco|"
            r"burger|pizza|bowl|skillet|sheet|casserole|curry|stir)\b).*$",
            re.I,
        ),
    ),
    (
        "Soups",
        re.compile(
            r"\b(soup|chowder|broth|gazpacho|bisque|pho|ramen|minestrone|"
            r"zuppa|stew|chili|chilli|dahl|dal|lentil\s+soup)\b",
            re.I,
        ),
    ),
    (
        "Breakfast",
        re.compile(
            r"\b(breakfast|overnight\s*oats|porridge|pancakes?|waffles?|"
            r"french\s*toast|scramble|frittata|parfait|chia\s*pudding|"
            r"egg\s*muffin|bagels?|hash\s*browns?|crepes?|eggs\s*benedict|"
            r"granola(?!\s*bar)|oatmeal(?!\s*cookie)|steel[\s-]?cut\s*oats|"
            r"breakfast\s*(burrito|casserole|sandwich|cookies?|bowl|hash))\b",
            re.I,
        ),
    ),
    (
        "Baking & Sweet Treats",
        re.compile(
            r"\b(muffins?|cookies?|biscuits?|cakes?|cupcakes?|scones?|"
            r"brownies?|blondies?|shortbread|donuts?|doughnuts?|loaf|"
            r"cinnamon\s*rolls?|pie|tart|crumble|cobbler|cracker|"
            r"bread(?!\s*pudding)|cornbread|naan|dinner\s*rolls?|"
            r"garlic\s*bread|fudge|panna\s*cotta|trifle|mousse|"
            r"cheesecake|custard|sugar\s*cookies?|lemon\s*bars?|"
            r"baked\s*oatmeal|chocolate\s*balls?|chokladbollar)\b",
            re.I,
        ),
    ),
    (
        "Sides & Salads",
        re.compile(
            r"\b((side|salad|slaw|coleslaw|fries|fritters?|tabbouleh|"
            r"mashed\s*potatoes|roasted\s*(carrots|potatoes|brussels|"
            r"parsnips|vegetables|broccoli|squash)|rice\s*pilaf|"
            r"polenta|stuffing|green\s*bean\s*casserole|"
            r"scalloped\s*potatoes|smashed\s*potatoes|"
            r"sweet\s*potato\s*(fries|casserole)|caprese|"
            r"potato\s*salad|pasta\s*salad|broccoli\s*salad|"
            r"cucumber\s*tomato|root\s*vegetables|"
            r"grilled\s*veggies|roasted\s*veg)\b)",
            re.I,
        ),
    ),
    (
        "Main Meals",
        re.compile(
            r"\b(chicken|beef|pork|salmon|tofu|tempeh|shrimp|turkey|tuna|"
            r"pasta|curry|tacos?|burrito|bowl|stir[\s-]?fry|lasagna|"
            r"casserole|risotto|pizza|burgers?|meatballs?|enchilada|"
            r"fajita|sheet[\s-]?pan|skillet|roast|pad\s*thai|"
            r"bolognese|alfredo|dinner|lunch|schnitzel|moussaka|"
            r"rendang|cordon\s*bleu|wings|spareribs|tostadas|"
            r"quesadilla|fish\s*fingers|scallops|stuffed\s*peppers|"
            r"wraps?|sandwich(?!\s*cookie)|hash(?!\s*brown))\b",
            re.I,
        ),
    ),
]

# Standalone sauce/dip titles (even if "chicken" appears in dip name)
SAUCE_FORCE = re.compile(
    r"\b(pizza\s*sauce|pasta\s*sauce|salad\s*dressing|vinaigrette|"
    r"marinade|hummus|tzatziki|pesto(?!\s+pasta)|gravy|ketchup|"
    r"chimichurri|ranch\s*dressing|garlic[\s-]?infused\s*olive\s*oil|"
    r"taco\s*seasoning|bbq\s*sauce|stir[\s-]?fry\s*sauce|"
    r"cranberry\s*sauce|cocktail\s*sauce)\b",
    re.I,
)


def classify(name: str, description: str = "") -> tuple[str, str]:
    m = re.match(
        r"^(Meal|Soup|Breakfast|Sauce|Smoothie|Mocktail|Sweet|Snack|Side)\s*[-–—]\s*",
        name,
        re.I,
    )
    if m:
        return FT_PREFIX[m.group(1).lower()], "ft-prefix"

    # Force clear condiment titles
    if SAUCE_FORCE.search(name) and not re.search(
        r"\b(chicken|beef|pork|salmon|shrimp|tofu|pasta\s+with|pizza\s+with)\b",
        name,
        re.I,
    ):
        return "Sauces", "sauce-force"

    blob = name  # title-first for cleaner counts; description adds noise
    for typ, rx in RULES:
        if rx.search(blob):
            return typ, "keyword"

    # light description fallback
    desc = (description or "")[:300]
    if desc:
        for typ, rx in RULES:
            if rx.search(desc):
                return typ, "description"

    return "Uncategorised", "none"


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=H) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        lf_folders = {
            c["uid"].lower(): c["name"]
            for c in cats
            if (c.get("parent_uid") or "").lower() == LF.lower()
            and c.get("name") != "Needs LF review"
        }

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=H) as r:
            index = (await r.json())["result"]

        by_type: Counter[str] = Counter()
        by_reason: Counter[str] = Counter()
        uncat_examples: list[tuple[str, str]] = []
        total = 0
        sem = asyncio.Semaphore(5)

        async def one(uid: str) -> None:
            nonlocal total
            async with sem:
                for attempt in range(6):
                    async with s.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
                    ) as r:
                        if r.status == 429:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        rec = (await r.json(content_type=None)).get("result") or {}
                        break
                else:
                    return
            if rec.get("in_trash"):
                return
            cats_l = {str(c).lower() for c in (rec.get("categories") or [])}
            hit = [lf_folders[c] for c in cats_l if c in lf_folders]
            if not hit:
                return
            total += 1
            name = rec.get("name") or ""
            typ, reason = classify(name, rec.get("description") or "")
            by_type[typ] += 1
            by_reason[reason] += 1
            if typ == "Uncategorised" and len(uncat_examples) < 40:
                uncat_examples.append((name, hit[0]))

        for start in range(0, len(index), 40):
            await asyncio.gather(*(one(e["uid"]) for e in index[start : start + 40]))
            await asyncio.sleep(0.35)

        print(f"Total Low Fodmap recipes (website folders): {total}\n")
        print("Counts by type (dry-run, no changes):")
        assigned = 0
        for typ in TYPES:
            n = by_type.get(typ, 0)
            assigned += n
            pct = (100.0 * n / total) if total else 0
            print(f"  {n:4d}  ({pct:4.1f}%)  {typ}")
        n_uncat = by_type.get("Uncategorised", 0)
        print(f"  {n_uncat:4d}  ({100.0 * n_uncat / total:4.1f}%)  Uncategorised")
        print(f"\nAssigned to a type: {assigned} / {total}")
        print("Signals:", dict(by_reason))
        print("\nSample Uncategorised:")
        for name, folder in uncat_examples:
            print(f"  [{folder}] {name}")


if __name__ == "__main__":
    asyncio.run(main())
