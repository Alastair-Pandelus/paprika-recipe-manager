"""Explore FODMAP Tracker recipes site structure (sections + See all)."""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from urllib.parse import urljoin

import aiohttp

BASE = "https://fodmaptracker.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaprikaImporter/1.0)"}


async def fetch(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as r:
        print(f"GET {r.status} {url}")
        return await r.text()


async def main() -> None:
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        html = await fetch(session, f"{BASE}/recipes/")

        # Section headings and nearby See all links
        # Look for ## Breakfast style and href patterns
        sections = re.findall(
            r'<h2[^>]*>\s*(.*?)\s*</h2>(.*?)(?=<h2|$)',
            html,
            re.I | re.S,
        )
        print(f"\nh2 sections found: {len(sections)}")
        for title, body in sections:
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            see_alls = re.findall(r'href=["\']([^"\']+)["\'][^>]*>\s*See all', body, re.I)
            links = re.findall(r'href=["\'](/recipes/[^"\']+)["\']', body)
            links = sorted(set(links))
            print(f"\n=== {title_clean} ===")
            print(f"  see all: {see_alls}")
            print(f"  inline links: {len(links)}")
            for L in links[:5]:
                print(f"    {L}")

        # Also dump all /recipes/ links from homepage
        all_links = sorted(set(re.findall(r'href=["\'](/recipes/[^"\']+)["\']', html)))
        print(f"\nAll /recipes/ links on index: {len(all_links)}")
        for L in all_links:
            print(L)

        # Probe a few see-all style category URLs
        candidates = [
            f"{BASE}/recipes/breakfast/",
            f"{BASE}/recipes/meals/",
            f"{BASE}/recipes/sides/",
            f"{BASE}/recipes/soups/",
            f"{BASE}/recipes/snacks/",
            f"{BASE}/recipes/sweets/",
            f"{BASE}/recipes/sauces/",
            f"{BASE}/recipes/sauces-condiments/",
            f"{BASE}/recipes/smoothies/",
            f"{BASE}/recipes/mocktails/",
            f"{BASE}/recipes/category/soups/",
            f"{BASE}/recipes/category/sides/",
        ]
        print("\n--- probing category URLs ---")
        for url in candidates:
            async with session.get(url) as r:
                text = await r.text() if r.status == 200 else ""
                n = len(set(re.findall(r'href=["\'](/recipes/[^"\']+)["\']', text)))
                print(f"  {r.status} links={n}  {url}")

        # Sample recipe page fields
        sample = f"{BASE}/recipes/low-fodmap-potato-leek-soup/"
        page = await fetch(session, sample)
        print("\n--- sample recipe markers ---")
        for pat in [
            "Ingredients",
            "Directions",
            "Instructions",
            "Method",
            "og:image",
            "Prep",
            "Servings",
            "application/ld+json",
        ]:
            print(f"  {pat}: {pat.lower() in page.lower()}")
        # extract ld+json if present
        ld = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page,
            re.I | re.S,
        )
        print(f"ld+json blocks: {len(ld)}")
        if ld:
            print(ld[0][:1500])


if __name__ == "__main__":
    asyncio.run(main())
