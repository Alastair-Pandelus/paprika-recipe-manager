"""Dry-run: map Low Fodmap recipes onto a proposed type list (no writes)."""
from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

RULES = [
    (
        "Bars",
        re.compile(
            r"\b(granola\s*bars?|energy\s*balls?|energy\s*bars?|protein\s*bars?|"
            r"flapjack|no[\s-]?bake\s*bars?|snack\s*bars?)\b",
            re.I,
        ),
    ),
    (
        "Ice Cream and Frozen Desserts",
        re.compile(
            r"\b(ice\s*cream|gelato|sorbet|frozen\s*yogurt|popsicle|magnum|"
            r"nice\s*cream|frozen\s*dessert)\b",
            re.I,
        ),
    ),
    (
        "Smoothies and Drinks",
        re.compile(
            r"\b(smoothie|mocktail|latte|lemonade|iced\s*(coffee|matcha)|"
            r"hot\s*chocolate|turmeric\s*latte|soda|juice|cider|eggnog|"
            r"margarita|mojito|sangria|pina\s*colada|drink|beverage)\b",
            re.I,
        ),
    ),
    (
        "Soups",
        re.compile(
            r"\b(soup|stew|chowder|broth|dahl|dal|chili|chilli|gazpacho|"
            r"bisque|pho|ramen|minestrone|zuppa)\b",
            re.I,
        ),
    ),
    (
        "Sauces",
        re.compile(
            r"\b(sauce|dressing|vinaigrette|pesto|marinade|mayo|mayonnaise|"
            r"salsa|gravy|seasoning|chimichurri|ketchup|relish|aioli|"
            r"hummus|tzatziki|ranch|condiment)\b",
            re.I,
        ),
    ),
    (
        "Breakfast",
        re.compile(
            r"\b(breakfast|overnight\s*oats|porridge|oatmeal(?!\s*cookie)|"
            r"pancake|waffle|french\s*toast|scramble|frittata|"
            r"granola(?!\s*bar)|parfait|chia\s*pudding|egg\s*muffin|"
            r"bagel|hash\s*brown|crepe|eggs\s*benedict|toast)\b",
            re.I,
        ),
    ),
    (
        "Baking",
        re.compile(
            r"\b(muffin|cookie|biscuit|cake|cupcake|bread(?!\s*pudding)|"
            r"scone|brownie|shortbread|donut|doughnut|loaf|"
            r"cinnamon\s*roll|pie|tart|crumble|blondie|cracker|"
            r"baked\s*oatmeal|cookie)\b",
            re.I,
        ),
    ),
    (
        "Main Meals",
        re.compile(
            r"\b(chicken|beef|pork|salmon|tofu|tempeh|shrimp|turkey|pasta|"
            r"curry|taco|bowl|stir[\s-]?fry|lasagna|casserole|risotto|"
            r"pizza|burger|meatball|enchilada|fajita|sheet[\s-]?pan|"
            r"skillet|roast|pad\s*thai|bolognese|alfredo|dinner|lunch|"
            r"salad|side|fries|potato|rice|noodles|gnocchi|dumpling)\b",
            re.I,
        ),
    ),
]

FT_MAP = {
    "meal": "Main Meals",
    "soup": "Soups",
    "breakfast": "Breakfast",
    "sauce": "Sauces",
    "smoothie": "Smoothies and Drinks",
    "mocktail": "Smoothies and Drinks",
    "sweet": "Baking",
    "snack": "UNCLEAR_SNACK",
    "side": "UNCLEAR_SIDE",
}


def classify(name: str, description: str = "") -> tuple[str, str]:
    m = re.match(
        r"^(Meal|Soup|Breakfast|Sauce|Smoothie|Mocktail|Sweet|Snack|Side)\s*[-–—]\s*",
        name,
        re.I,
    )
    if m:
        pref = m.group(1).lower()
        return FT_MAP.get(pref, "UNMAPPED"), f"prefix:{pref}"

    blob = f"{name}\n{(description or '')[:400]}"
    hits = []
    for typ, rx in RULES:
        if rx.search(blob):
            hits.append(typ)
    seen = set()
    hits = [h for h in hits if not (h in seen or seen.add(h))]
    if len(hits) == 1:
        return hits[0], "keyword"
    if len(hits) > 1:
        return "AMBIGUOUS:" + ",".join(hits[:4]), "multi"
    return "UNCLASSIFIED", "none"


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
        lf_folders = {}
        for c in cats:
            if (c.get("parent_uid") or "").lower() == LF.lower():
                lf_folders[c["uid"].lower()] = c["name"]

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=H) as r:
            index = (await r.json())["result"]

        by_type: Counter = Counter()
        folder_counts: Counter = Counter()
        prefix_counts: Counter = Counter()
        ambiguous_examples = []
        unclassified_examples = []
        unclear_side = []
        unclear_snack = []
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
            hit_folders = [
                lf_folders[c]
                for c in cats_l
                if c in lf_folders and lf_folders[c] not in {"Needs LF review"}
            ]
            if not hit_folders:
                return
            total += 1
            for f in hit_folders:
                folder_counts[f] += 1
            name = rec.get("name") or ""
            typ, reason = classify(name, rec.get("description") or "")
            by_type[typ] += 1
            if reason.startswith("prefix:"):
                prefix_counts[reason] += 1
            if typ == "UNCLEAR_SIDE" and len(unclear_side) < 30:
                unclear_side.append((name, hit_folders[0]))
            if typ == "UNCLEAR_SNACK" and len(unclear_snack) < 30:
                unclear_snack.append((name, hit_folders[0]))
            if typ.startswith("AMBIGUOUS") and len(ambiguous_examples) < 35:
                ambiguous_examples.append((name, typ, hit_folders[0]))
            elif typ == "UNCLASSIFIED" and len(unclassified_examples) < 40:
                unclassified_examples.append((name, hit_folders[0]))

        for start in range(0, len(index), 40):
            await asyncio.gather(*(one(e["uid"]) for e in index[start : start + 40]))
            await asyncio.sleep(0.35)

        print(f"Total LF website-folder recipes: {total}")
        print("\nBy folder:")
        for f, n in folder_counts.most_common():
            print(f"  {n:4d}  {f}")
        print("\nProposed-type dry-run:")
        for t, n in by_type.most_common():
            print(f"  {n:4d}  {t}")
        print("\nFodmap Tracker prefixes:")
        for p, n in prefix_counts.most_common():
            print(f"  {n:4d}  {p}")
        print("\nUNCLEAR_SIDE samples (FT Side - …):")
        for name, folder in unclear_side:
            print(f"  [{folder}] {name}")
        print("\nUNCLEAR_SNACK samples (FT Snack - …):")
        for name, folder in unclear_snack:
            print(f"  [{folder}] {name}")
        print("\nAMBIGUOUS samples:")
        for name, typ, folder in ambiguous_examples[:25]:
            print(f"  [{folder}] {name} -> {typ}")
        print("\nUNCLASSIFIED samples:")
        for name, folder in unclassified_examples[:35]:
            print(f"  [{folder}] {name}")


if __name__ == "__main__":
    asyncio.run(main())
