"""Show how water appears on FD labels vs allocated amounts."""
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

# Import allocator from rescale script by exec of helpers only — simpler: duplicate minimal extract
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
    s = s.replace("Enzymes}", "Enzymes]")
    return re.sub(r"\s+", " ", s).strip()


async def main() -> None:
    load_env()
    # load rescale module functions
    import importlib.util
    import sys
    import types

    path = r"C:\Users\Pandelus\AppData\Local\Temp\fd_rescale_density.py"
    mod = types.ModuleType("fd")
    sys.modules["fd"] = mod
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), mod.__dict__)

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

        print(f"{'recipe':55} {'label':12} {'alloc%':>8} {'g/8port':>8}  context")
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            url = recipe.get("source_url")
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            raw, serving = mod.extract_from_html(html)
            if not raw:
                continue
            items = mod.merge_duplicate_ingredients(
                mod.allocate_percentages(mod.parse_ingredients(raw))
            )
            water = next((it for it in items if re.match(r"(?i)^water\b", it.name)), None)
            if not water:
                continue
            # context: neighbours on raw label
            m = re.search(r"(?i)(.{0,40}\bwater\b.{0,40})", raw)
            ctx = (m.group(1).replace("\n", " ") if m else "")[:70]
            label = f"{water.pct:g}%" if water.pct is not None else "unlabelled"
            g = serving * 8 * water.assigned / 100
            print(
                f"{(recipe.get('name') or '')[:55]:55} {label:12} {water.assigned:8.2f} {g:8.0f}  {ctx}"
            )


asyncio.run(main())
