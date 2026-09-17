"""Dry-run: estimate vegan Low Fodmap recipes (no writes)."""
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

VEGAN_TITLE = re.compile(
    r"\b(vegan|plant[\s-]?based|vegetarian(?!\s+enchilada))\b",  # vegetarian ≠ vegan; keep separate
    re.I,
)
VEGAN_STRICT_TITLE = re.compile(r"\b(vegan|plant[\s-]?based)\b", re.I)
VEGETARIAN_TITLE = re.compile(r"\bvegetarian\b", re.I)

ANIMAL = re.compile(
    r"\b(chicken|beef|pork|turkey|lamb|bacon|sausage|shrimp|prawn|salmon|"
    r"tuna|cod|fish|anchovy|meatball|ground\s+meat|mince|"
    r"egg\b|eggs\b|butter(?!\s*bean)|cheese|parmesan|feta|mozzarella|"
    r"ricotta|yogurt|yoghurt|cream(?!\s*of\s*tartar)|milk|"
    r"honey|gelatin|worcestershire|fish\s*sauce)\b",
    re.I,
)

# dairy/egg free plant signals
PLANT_OK = re.compile(
    r"\b(tofu|tempeh|lentil|chickpea|quinoa|oat|almond\s*milk|"
    r"coconut\s*milk|maple|vegan)\b",
    re.I,
)


def classify_vegan(name: str, ingredients: str) -> str:
    if VEGAN_STRICT_TITLE.search(name):
        return "vegan_labelled"
    if VEGETARIAN_TITLE.search(name) and not VEGAN_STRICT_TITLE.search(name):
        return "vegetarian_labelled"
    ing = ingredients or ""
    # no strong animal words in name+ingredients
    blob = f"{name}\n{ing}"
    if ANIMAL.search(blob):
        return "contains_animal"
    # plant-leaning or empty-ish ingredients
    if PLANT_OK.search(blob) or len(ing.strip()) > 40:
        # ambiguous: no animal detected
        return "likely_vegan_no_animal_words"
    return "unknown"


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

        by = Counter()
        by_folder_labelled = Counter()
        examples = {k: [] for k in [
            "vegan_labelled", "vegetarian_labelled", "likely_vegan_no_animal_words"
        ]}
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
            label = classify_vegan(name, rec.get("ingredients") or "")
            by[label] += 1
            if label == "vegan_labelled":
                by_folder_labelled[hit[0]] += 1
            if label in examples and len(examples[label]) < 8:
                examples[label].append((name, hit[0]))

        for start in range(0, len(index), 40):
            await asyncio.gather(*(one(e["uid"]) for e in index[start : start + 40]))
            await asyncio.sleep(0.35)

        print(f"Total LF recipes: {total}\n")
        for k, n in by.most_common():
            print(f"  {n:4d}  ({100*n/total:4.1f}%)  {k}")
        print("\nVegan-labelled by source folder:")
        for f, n in by_folder_labelled.most_common():
            print(f"  {n:4d}  {f}")
        for k, rows in examples.items():
            print(f"\nSamples {k}:")
            for name, folder in rows:
                print(f"  [{folder}] {name}")


if __name__ == "__main__":
    asyncio.run(main())
