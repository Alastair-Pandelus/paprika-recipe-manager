"""Check Field Doctor recipes: active vs trash."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> None:
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
            index = (await r.json())["result"]
        print("index count", len(index))

        active = []
        trashed = []
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=H
            ) as r:
                recipe = (await r.json()).get("result") or {}
            name = recipe.get("name") or ""
            cats = recipe.get("categories") or []
            url = recipe.get("source_url") or ""
            src = recipe.get("source") or ""
            blob = f"{name} {url} {src}".lower()
            if "field doctor" in blob or "fielddoctor" in blob or FD in cats:
                item = {
                    "name": name,
                    "uid": recipe.get("uid"),
                    "in_trash": bool(recipe.get("in_trash")),
                    "photo": bool(recipe.get("photo_url")),
                    "categories": cats,
                }
                if item["in_trash"]:
                    trashed.append(item)
                else:
                    active.append(item)

        print("FD active", len(active))
        print("FD trashed", len(trashed))
        print("\n=== ACTIVE ===")
        for x in sorted(active, key=lambda t: t["name"]):
            print(f"  {x['name']}")
        print("\n=== TRASHED ===")
        for x in sorted(trashed, key=lambda t: t["name"]):
            print(f"  {x['name']} | {x['uid']}")


asyncio.run(main())
