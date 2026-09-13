"""Fetch FD recipes: ingredients + directions sample for water handling."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import aiohttp

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> None:
    load_env()
    out = []
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
            ings = recipe.get("ingredients") or ""
            has_water = bool(re.search(r"(?im)^.*\bwater\b", ings))
            out.append(
                {
                    "uid": recipe.get("uid"),
                    "name": recipe.get("name"),
                    "has_water_ing": has_water,
                    "ingredients": ings,
                    "directions": recipe.get("directions") or "",
                }
            )
    Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_dirs.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print(len(out), "recipes;", sum(1 for x in out if x["has_water_ing"]), "with water ing")


asyncio.run(main())
