"""Diagnose missing classic porridge, photo attach, peanut ings."""
from __future__ import annotations

import asyncio
import html as html_lib
import io
import os
import re
from pathlib import Path

import aiohttp
from PIL import Image

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"


async def main():
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        h = {"Authorization": f"Bearer {token}"}
        async with s.get(f"{BASE}/v2/sync/recipes/", headers=h) as r:
            index = (await r.json())["result"]
        print("FD recipes:")
        for e in index:
            async with s.get(f"{BASE}/v2/sync/recipe/{e['uid']}/", headers=h) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            name = recipe.get("name") or ""
            if any(
                x in name.lower()
                for x in ("porridge", "bar", "chocolate", "peanut", "lemon", "banana", "hazelnut")
            ):
                print(
                    "-",
                    name,
                    "| trash",
                    recipe.get("in_trash"),
                    "| photo",
                    bool(recipe.get("photo") or recipe.get("photo_url")),
                    "| url",
                    recipe.get("source_url"),
                    "| ings lines",
                    len((recipe.get("ingredients") or "").splitlines()),
                )
                if "Peanut Choc" in name:
                    print(recipe.get("ingredients"))
                    print("RAW notes source tail:", (recipe.get("notes") or "")[-400:])

        # test photo download
        url = "https://cdn.shopify.com/s/files/1/0271/2662/8450/files/IMG-8118.png?v=1759516851"
        async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            print("cdn status", r.status, "len", len(await r.read()))

        # peanut raw extract
        async with s.get(
            "https://www.fielddoctor.co.uk/products/peanut-choc-chunk",
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            html = await r.text()
        key = 'ingredient_list\\",\\"'
        i = html.find(key)
        start = i + len(key)
        end = html.find('\\",\\"', start)
        raw = html[start:end]
        text = raw.encode("utf-8").decode("unicode_escape")
        text = html_lib.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        print("PEANUT CLEAN:", re.sub(r"\s+", " ", text).strip())


asyncio.run(main())
