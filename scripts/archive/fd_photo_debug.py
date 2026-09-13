import asyncio, gzip, hashlib, json, os, io
from datetime import datetime
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
        good = bad = None
        for e in index:
            async with s.get(f"{BASE}/v2/sync/recipe/{e['uid']}/", headers=h) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            if "Bolognese" in (recipe.get("name") or "") and recipe.get("photo"):
                good = recipe
            if "Cinnamon Porridge" in (recipe.get("name") or ""):
                bad = recipe
        print("GOOD photo fields:")
        for k in ("photo", "photo_hash", "photo_large", "photo_url", "image_url"):
            print(f"  {k}={good.get(k)!r}")
        print("BAD photo fields:")
        for k in ("photo", "photo_hash", "photo_large", "photo_url", "image_url"):
            print(f"  {k}={bad.get(k)!r}")

        # Try attach photo using ONLY photo_upload + minimal hash bump like meals
        url = "https://cdn.shopify.com/s/files/1/0271/2662/8450/files/IMG-8118.png?v=1759516851"
        async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            raw = await r.read()
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((800, 800))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=75)
        jpeg = buf.getvalue()
        print("jpeg bytes", len(jpeg))

        # Approach: copy photo field names empty string vs None
        recipe = bad
        recipe["photo"] = ""
        recipe["photo_hash"] = ""
        recipe["photo_large"] = ""
        recipe["photo_url"] = ""
        recipe["image_url"] = ""
        recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipe["hash"] = calc_hash(recipe)
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip.compress(json.dumps(recipe, separators=(",", ":")).encode()),
            filename="data",
            content_type="application/octet-stream",
        )
        form.add_field("photo_upload", jpeg, filename="photo.jpg", content_type="image/jpeg")
        async with s.post(
            f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=h, data=form
        ) as r:
            print("empty-string approach", await r.text())

        # Approach 2: omit photo keys entirely
        async with s.get(f"{BASE}/v2/sync/recipe/{bad['uid']}/", headers=h) as r:
            recipe = (await r.json())["result"]
        for k in ("photo", "photo_hash", "photo_large", "photo_url", "image_url"):
            recipe.pop(k, None)
        recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipe["hash"] = calc_hash(recipe)
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip.compress(json.dumps(recipe, separators=(",", ":")).encode()),
            filename="data",
            content_type="application/octet-stream",
        )
        form.add_field("photo_upload", jpeg, filename="photo.jpg", content_type="image/jpeg")
        async with s.post(
            f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=h, data=form
        ) as r:
            print("omit-keys approach", await r.text())


asyncio.run(main())
