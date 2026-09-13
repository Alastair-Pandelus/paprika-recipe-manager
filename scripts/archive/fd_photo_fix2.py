import asyncio
import gzip
import hashlib
import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
from PIL import Image

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

USER = os.environ["PAPRIKA_USERNAME"]
PASS = os.environ["PAPRIKA_PASSWORD"]
BASE = "https://paprikaapp.com/api"
CDN = "https://cdn.shopify.com/s/files/1/0271/2662/8450/files/Cinnamon_Porridge.jpg?v=1701872831"


def calc_hash(obj):
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{BASE}/v1/account/login", data={"email": USER, "password": PASS}
        ) as r:
            tok = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {tok}"}
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=H) as r:
            items = (await r.json())["result"]
        uid = [
            i["uid"]
            for i in items
            if i.get("name") == "Field Doctor-Style Cinnamon Porridge (Low FODMAP)"
        ][0]
        async with session.get(f"{BASE}/v2/sync/recipe/{uid}/", headers=H) as r:
            recipe = (await r.json())["result"]
        print(
            "before",
            {
                k: recipe.get(k)
                for k in ("photo", "photo_hash", "photo_large", "photo_url", "image_url", "photos")
            },
        )

        async with session.get(CDN) as r:
            raw = await r.read()
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        jpeg = buf.getvalue()
        print("jpeg", len(jpeg))

        photo_name = str(uuid.uuid4()).upper() + ".jpg"
        photo_hash = hashlib.sha256(jpeg).hexdigest().upper()

        # Match paprika_client: set photo + photo_hash before upload
        recipe2 = dict(recipe)
        recipe2["photo"] = photo_name
        recipe2["photo_large"] = photo_name
        recipe2["photo_hash"] = photo_hash
        recipe2["image_url"] = CDN
        recipe2["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipe2["hash"] = calc_hash(recipe2)

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip.compress(json.dumps(recipe2).encode()),
            content_type="application/octet-stream",
            filename="data",
        )
        form.add_field(
            "photo_upload", jpeg, filename=photo_name, content_type="image/jpeg"
        )
        async with session.post(
            f"{BASE}/v2/sync/recipe/{uid}/", headers=H, data=form
        ) as r:
            body = await r.text()
            print("upload", body[:300])

        async with session.get(f"{BASE}/v2/sync/recipe/{uid}/", headers=H) as r:
            recipe = (await r.json())["result"]
        print(
            "after",
            {
                k: recipe.get(k)
                for k in ("photo", "photo_hash", "photo_large", "photo_url", "image_url")
            },
        )
        print("has_url", bool(recipe.get("photo_url")))


asyncio.run(main())
