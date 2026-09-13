"""Attach Shopify product photos to all 9 FD non-meal recipes."""
from __future__ import annotations

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

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
COLL = "https://www.fielddoctor.co.uk/collections/low-fodmap/products.json?limit=250"
HANDLES = [
    "porridge-classic",
    "porridge-cinnamon",
    "porridge-cherry-chocolate",
    "no-sugar-banana-porridge",
    "double-chocolate-bar",
    "peanut-choc-chunk",
    "lemon-coconut-bar",
    "banana-peanut-butter-bar",
    "hazelnut-mocha-bar",
]


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def to_jpeg(raw: bytes, max_side: int = 1000) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


async def main() -> None:
    load_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.get(COLL, headers={"User-Agent": "Mozilla/5.0"}) as r:
            products = {p["handle"]: p for p in (await r.json())["products"]}

        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        by_url: dict[str, dict] = {}
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            url = recipe.get("source_url") or ""
            if any(f"/products/{h}" in url for h in HANDLES):
                by_url[url] = recipe

        for handle in HANDLES:
            source_url = f"https://www.fielddoctor.co.uk/products/{handle}"
            recipe = by_url.get(source_url)
            if not recipe:
                print("MISSING", handle)
                continue
            p = products.get(handle)
            if not p or not p.get("images"):
                print("NO IMAGE", handle)
                continue
            cdn = p["images"][0]["src"]
            async with session.get(cdn) as r:
                raw = await r.read()
            jpeg = to_jpeg(raw)
            photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
            photo_hash = hashlib.sha256(jpeg).hexdigest().upper()

            recipe["photo"] = photo_name
            recipe["photo_large"] = photo_name
            recipe["photo_hash"] = photo_hash
            recipe["photo_url"] = None
            recipe["image_url"] = None
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe["hash"] = calc_hash(recipe)

            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(recipe),
                content_type="application/octet-stream",
                filename="data",
            )
            form.add_field(
                "photo_upload",
                jpeg,
                filename=photo_name,
                content_type="image/jpeg",
            )
            async with session.post(
                f"{BASE}/v2/sync/recipe/{recipe['uid']}/",
                headers=headers,
                data=form,
            ) as r:
                body = await r.text()
                ok = '"result":true' in body.replace(" ", "")

            if ok:
                async with session.get(
                    f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers
                ) as r:
                    rr = (await r.json())["result"]
                print(
                    "OK",
                    recipe["name"],
                    "photo=",
                    rr.get("photo"),
                    "url=",
                    bool(rr.get("photo_url")),
                    f"jpeg={len(jpeg)}",
                )
            else:
                print("FAIL", recipe["name"], body[:220])

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("notify done")


asyncio.run(main())
