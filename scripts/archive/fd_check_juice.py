import asyncio, os
from pathlib import Path
import aiohttp

for line in Path(r"C:\Users\Pandelus\.paprika-mcp.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

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
        for e in index:
            async with s.get(f"https://paprikaapp.com/api/v2/sync/recipe/{e['uid']}/", headers=h) as r:
                recipe = (await r.json()).get("result") or {}
            ings = recipe.get("ingredients") or ""
            if "juice of" not in ings.lower():
                continue
            for line in ings.splitlines():
                if "juice of" in line.lower():
                    print(f"{recipe.get('name','')[:55]} -> {line}")

asyncio.run(main())
