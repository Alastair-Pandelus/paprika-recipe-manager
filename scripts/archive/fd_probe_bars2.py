"""Find working product URLs and ingredient extraction for bars."""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import re

import aiohttp

HANDLES = [
    "double-chocolate-bar",
    "peanut-choc-chunk",
    "lemon-coconut-bar",
    "banana-peanut-butter-bar",
    "hazelnut-mocha-bar",
]


def clean(s: str) -> str:
    s = s.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u003cb\\u003e", "").replace("\\u003c/b\\u003e", "")
    s = s.replace("\\u003cstrong\\u003e", "").replace("\\u003c/strong\\u003e", "")
    s = s.replace("\\t", " ").replace('\\"', '"')
    s = html_lib.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        # collection for images/handles
        async with session.get(
            "https://www.fielddoctor.co.uk/collections/low-fodmap/products.json?limit=250",
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            products = (await r.json())["products"]
        by_handle = {p["handle"]: p for p in products}

        for h in HANDLES:
            p = by_handle.get(h)
            print("=" * 60, h)
            if not p:
                print("NOT IN COLLECTION")
                continue
            print("title", p.get("title"))
            print("img", (p.get("images") or [{}])[0].get("src", "")[:80])
            url = f"https://www.fielddoctor.co.uk/products/{h}"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True) as r:
                final = str(r.url)
                html = await r.text()
            print("final url", final)
            # try several patterns
            patterns = [
                r'ingredient_list\\",\\"(.*?)\\"',
                r'"ingredient_list":"(.*?)"',
                r"(?i)ingredients?\s*</[^>]+>\s*<[^>]+>(.*?)</",
                r"(?i)ingredients?\s*:?\s*</?(?:p|div|strong|b)[^>]*>\s*(.*?)(?:</p>|for allergens|nutrition)",
            ]
            found = None
            for pat in patterns:
                m = re.search(pat, html, re.I | re.S)
                if m:
                    found = clean(m.group(1))[:400]
                    print("PAT", pat[:40], "->", found[:200])
                    break
            if not found:
                # dump nearby 'ingredient'
                idx = html.lower().find("ingredient")
                print("no parse; context:", clean(html[max(0, idx) : idx + 500])[:300] if idx >= 0 else "none")


asyncio.run(main())
