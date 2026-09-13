"""Move granola bars to Curated and attach Desiree Nielsen photo."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

UID = "7F6F4917-B01F-4B51-A747-F2A3E369054C"
CURATED = "D48E4AA7-676D-4BE9-BB72-1E8894371385"
PAGE = "https://desireerd.com/one-bowl-low-fodmap-granola-bar/"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def to_jpeg(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=headers) as r:
            rec = (await r.json())["result"]
        print("before photo", rec.get("photo"), bool(rec.get("photo_url")))

        async with session.get(PAGE, headers={"User-Agent": "Mozilla/5.0"}) as r:
            html = await r.text()
        m = re.search(
            r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I
        )
        if not m:
            m = re.search(
                r'content=["\'](https://[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
                html,
                re.I,
            )
        img_url = m.group(1) if m else None
        print("og image", img_url)

        jpeg = None
        if img_url:
            async with session.get(img_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                raw = await r.read()
                status = r.status
            print("img", status, len(raw))
            if status == 200 and len(raw) > 1000:
                jpeg = to_jpeg(raw)

        rec["categories"] = [CURATED]
        rec["source"] = "Desiree Nielsen RD"
        rec["source_url"] = PAGE
        if jpeg:
            photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
            photo_hash = hashlib.sha256(jpeg).hexdigest().upper()
            rec["photo"] = photo_name
            rec["photo_large"] = photo_name
            rec["photo_hash"] = photo_hash
            rec["photo_url"] = None
            rec["image_url"] = None
        rec["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rec["hash"] = calc_hash(rec)

        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(rec),
            content_type="application/octet-stream",
            filename="data",
        )
        if jpeg:
            form.add_field(
                "photo_upload", jpeg, filename=rec["photo"], content_type="image/jpeg"
            )
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=headers, data=form
        ) as r:
            print("post", await r.text())

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=headers) as r:
            rr = (await r.json())["result"]
        print(
            "final",
            rr.get("name"),
            rr.get("categories"),
            "photo=",
            bool(rr.get("photo_url")),
        )


if __name__ == "__main__":
    asyncio.run(main())
