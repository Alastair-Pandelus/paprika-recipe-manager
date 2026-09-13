"""Review EVOO role across Field Doctor recipes: amount, label position, directions use."""
from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
import sys
import types
from pathlib import Path

import aiohttp

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
SCRIPT = Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_rescale_density.py")


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> None:
    load_env()
    mod = types.ModuleType("fd")
    sys.modules["fd"] = mod
    exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), mod.__dict__)

    rows = []
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
            url = recipe.get("source_url")
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            raw, serving = mod.extract_from_html(html)
            if not raw:
                continue
            items = mod.merge_duplicate_ingredients(
                mod.allocate_percentages(mod.parse_ingredients(raw))
            )
            oils = [
                it
                for it in items
                if re.search(r"(?i)olive oil|extra virgin", primary := mod.primary_name(it.name))
                and re.search(r"(?i)oil", primary)
            ]
            dirs = recipe.get("directions") or ""
            # How directions mention oil
            fryish = len(
                re.findall(
                    r"(?i)(brown|fry|soften|sear|stir-fry|saute|sauté|roast).{0,40}oil|oil.{0,40}(brown|fry|soften|sear|stir-fry|saute|sauté|roast)",
                    dirs,
                )
            )
            finishish = len(
                re.findall(
                    r"(?i)(drizzle|dress|pesto|finish|toss with).{0,30}oil|oil.{0,30}(drizzle|dress|pesto)",
                    dirs,
                )
            )
            oil_mentions = len(re.findall(r"(?i)olive oil|EVOO", dirs))

            total_g = serving * 8
            for it in oils:
                g = total_g * it.assigned / 100.0
                tbsp = g / 13.5  # ~4.5 g/tsp * 3
                # label context
                m = re.search(
                    r"(?i)(.{0,35}(?:extra virgin olive oil|olive oil).{0,35})", raw
                )
                ctx = (m.group(1) if m else "")[:70]
                label = f"{it.pct:g}%" if it.pct is not None else "unlabelled"
                rows.append(
                    {
                        "name": recipe.get("name") or "",
                        "g": g,
                        "tbsp": tbsp,
                        "pct": it.assigned,
                        "label": label,
                        "fryish": fryish,
                        "finishish": finishish,
                        "oil_mentions": oil_mentions,
                        "ctx": ctx,
                        "dirs_snip": re.sub(r"\s+", " ", dirs)[:180],
                    }
                )

    rows.sort(key=lambda x: -x["g"])
    print(f"{'g/8':>5} {'tbsp':>5} {'%':>5} {'label':>10}  recipe")
    for r in rows:
        short = r["name"].replace("Field Doctor-Style ", "").replace(" (Low FODMAP)", "")[:42]
        print(f"{r['g']:5.0f} {r['tbsp']:5.1f} {r['pct']:5.2f} {r['label']:>10}  {short}")

    print("\n--- Classification hint (g per 8 portions) ---")
    print("cupboard/fry-scale (<~60g / ~4 tbsp): ", sum(1 for r in rows if r["g"] < 60))
    print("moderate (60–120g):                 ", sum(1 for r in rows if 60 <= r["g"] < 120))
    print("substantial ingredient (>=120g):     ", sum(1 for r in rows if r["g"] >= 120))
    print("\nTop contexts:")
    for r in rows[:8]:
        short = r["name"].replace("Field Doctor-Style ", "")[:40]
        print(f"- {short}: {r['g']:.0f}g | label:{r['label']} | dir oil refs:{r['oil_mentions']} fryish:{r['fryish']} finish:{r['finishish']}")
        print(f"  ctx: {r['ctx']}")


asyncio.run(main())
