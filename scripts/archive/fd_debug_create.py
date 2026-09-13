import asyncio, gzip, hashlib, json, os, uuid
from datetime import datetime
from pathlib import Path
import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"


def calc_hash(obj):
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


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
        template = None
        for e in index:
            async with s.get(f"{BASE}/v2/sync/recipe/{e['uid']}/", headers=h) as r:
                recipe = (await r.json()).get("result") or {}
            if FD in (recipe.get("categories") or []) and "Bolognese" in (
                recipe.get("name") or ""
            ):
                template = recipe
                break
        print("TEMPLATE KEYS", sorted(template.keys()))
        Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_recipe_template.json").write_text(
            json.dumps({k: template[k] for k in template if k not in ("ingredients", "directions", "notes")}, indent=2),
            encoding="utf-8",
        )

        # minimal create test
        uid = str(uuid.uuid4()).upper()
        recipe = {k: template[k] for k in template}
        recipe["uid"] = uid
        recipe["name"] = "TEMP FD Import Test — delete"
        recipe["ingredients"] = "100 g Test Oats"
        recipe["directions"] = "Mix."
        recipe["notes"] = "test"
        recipe["source_url"] = "https://www.fielddoctor.co.uk/products/porridge-classic"
        recipe["categories"] = [FD, LF]
        recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipe["photo"] = None
        recipe["photos"] = []
        recipe["photo_hash"] = None
        recipe["hash"] = calc_hash(recipe)
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip.compress(json.dumps(recipe, separators=(",", ":")).encode()),
            content_type="application/octet-stream",
            filename="data",
        )
        async with s.post(f"{BASE}/v2/sync/recipe/{uid}/", headers=h, data=form) as r:
            print("STATUS", r.status)
            print((await r.text())[:1000])


asyncio.run(main())
