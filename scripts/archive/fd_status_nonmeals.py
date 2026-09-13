import asyncio
import os
from pathlib import Path

import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = "https://paprikaapp.com/api"
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


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
        ) as r:
            tok = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {tok}"}
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=H) as r:
            items = (await r.json())["result"]
        print("total recipes", len(items))
        found = []
        for entry in items:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=H
            ) as r:
                recipe = (await r.json()).get("result") or {}
            url = recipe.get("source_url") or ""
            name = recipe.get("name") or ""
            if any(h in url for h in HANDLES) or (
                "Field Doctor-Style" in name
                and ("Porridge" in name or "Bar" in name or "bar" in name)
            ):
                found.append(recipe)
        print("non-meal matches", len(found))
        for recipe in sorted(found, key=lambda x: x.get("name") or ""):
            print(
                "-",
                recipe.get("name"),
                "| trash=",
                recipe.get("in_trash"),
                "| photo=",
                bool(recipe.get("photo_url")),
                "| ings_lines=",
                len((recipe.get("ingredients") or "").strip().splitlines()),
                "| url=",
                recipe.get("source_url"),
            )


asyncio.run(main())
