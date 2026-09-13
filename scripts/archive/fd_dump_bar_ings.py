"""Dump raw ingredient_list JSON field for broken bars."""
from __future__ import annotations

import asyncio
import html as html_lib
import re

import aiohttp

HANDLES = ["lemon-coconut-bar", "banana-peanut-butter-bar", "double-chocolate-bar"]


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        for h in HANDLES:
            async with session.get(
                f"https://www.fielddoctor.co.uk/products/{h}",
                headers={"User-Agent": "Mozilla/5.0"},
            ) as r:
                html = await r.text()
            # Find start of ingredient_list
            key = 'ingredient_list\\",\\"'
            i = html.find(key)
            print("=" * 60, h, "idx", i)
            if i < 0:
                key2 = 'ingredient_list\\":\\"'
                i = html.find(key2)
                print("alt idx", i)
            if i >= 0:
                chunk = html[i : i + 2500]
                print(repr(chunk[:1500]))
                print()
                # unescape then strip
                # extract with balanced approach: from after key until \",\\
                start = i + len(key)
                # find end marker \",\\"
                end = html.find('\\",\\"', start)
                raw = html[start:end] if end > start else html[start : start + 2000]
                text = raw.encode().decode("unicode_escape", errors="ignore")
                text = html_lib.unescape(text)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                print("CLEAN:", text[:500])
                print()


asyncio.run(main())
