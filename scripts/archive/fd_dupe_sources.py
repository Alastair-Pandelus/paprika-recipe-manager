"""Show source ingredient lists for recipes with dupes."""
from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
from pathlib import Path

import aiohttp

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
TARGETS = ("Field Green Risotto", "Smokey Chipotle")


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def clean_text(s: str) -> str:
    s = html_lib.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\\u003cb\\u003e", "").replace("\\u003c/b\\u003e", "")
    return re.sub(r"\s+", " ", s).strip()


async def main() -> None:
    load_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            name = recipe.get("name") or ""
            if not any(t in name for t in TARGETS):
                continue
            url = recipe.get("source_url")
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            m = re.search(r'ingredient_list\\",\\"(.*?)\\"', html)
            if not m:
                m = re.search(r'ingredients" class="prose">(.*?)</', html, re.I | re.S)
            raw = clean_text(m.group(1)) if m else "?"
            raw = re.split(r"(?i)for allergens|manufactured on a site", raw)[0]
            print("=" * 60)
            print(name)
            print("SOURCE:", raw)
            print()
            notes = recipe.get("notes") or ""
            sm = re.search(r"Source ingredients:\s*(.*)", notes, re.S)
            if sm:
                print("NOTES SOURCE:", sm.group(1)[:800])
            print()


asyncio.run(main())
