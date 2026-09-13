"""Probe FD porridge/bar product pages for servings + ingredients."""
from __future__ import annotations

import asyncio
import html as html_lib
import re

import aiohttp

HANDLES = [
    "porridge-classic",
    "porridge-cinnamon",
    "porridge-cherry-chocolate",
    "no-sugar-banana-porridge",
    "double-chocolate-bar",
    "peanut-choc-chunk",
    "lemon-coconut-bar",
    "banana-peanut-butter-bar",
    "hazelnut-mocha-bar",
]


def clean(s: str) -> str:
    s = html_lib.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        for h in HANDLES:
            url = f"https://www.fielddoctor.co.uk/products/{h}"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            m = re.search(r'ingredient_list\\",\\"(.*?)\\"', html)
            if not m:
                m = re.search(r'ingredients" class="prose">(.*?)</', html, re.I | re.S)
            ings = clean(m.group(1)) if m else "NO INGS"
            ings = re.split(r"(?i)for allergens|manufactured on a site", ings)[0]
            servings = [int(x) for x in re.findall(r"per (\d{2,4})g", html, re.I)]
            non100 = [s for s in servings if s != 100]
            serving = max(non100) if non100 else (max(servings) if servings else None)
            # title
            tm = re.search(r"<title>([^<]+)", html, re.I)
            title = clean(tm.group(1)) if tm else h
            print("=" * 60)
            print(h, "| serving_guess", serving, "| servings_found", servings[:8])
            print("title:", title[:80])
            print("ings:", ings[:300])
            print()


asyncio.run(main())
