import asyncio, os, re
from pathlib import Path
import aiohttp

for line in Path(r"C:\Users\Pandelus\.paprika-mcp.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://paprikaapp.com/api/v1/account/login",
            data={"email": os.environ["PAPRIKA_USERNAME"], "password": os.environ["PAPRIKA_PASSWORD"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        h = {"Authorization": f"Bearer {token}"}
        async with s.get("https://paprikaapp.com/api/v2/sync/recipes/", headers=h) as r:
            index = (await r.json())["result"]
        water_ings = 0
        tip_dirs = 0
        note_omit = 0
        n = 0
        for e in index:
            async with s.get(f"https://paprikaapp.com/api/v2/sync/recipe/{e['uid']}/", headers=h) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            n += 1
            for ln in (recipe.get("ingredients") or "").splitlines():
                if re.fullmatch(r"(?i)(?:\d+(?:\.\d+)?\s*g\s+)?water", ln.strip()):
                    water_ings += 1
                    print("WATER ING:", recipe.get("name"), ln)
            if "Liquid for home cooking:" in (recipe.get("directions") or ""):
                tip_dirs += 1
            if "Water omitted from ingredients" in (recipe.get("notes") or ""):
                note_omit += 1
        print(f"recipes={n} plain_water_ings={water_ings} tip_dirs={tip_dirs} note_omit={note_omit}")

asyncio.run(main())
