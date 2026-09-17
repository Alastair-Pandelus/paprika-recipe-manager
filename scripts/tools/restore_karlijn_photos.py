"""Restore missing photos for two Karlijn Kitchen recipes from source og:image."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import re
import sys
import uuid
from pathlib import Path

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402

UIDS = [
    "3EFE6939-7E62-480A-BBFF-C1896E8AEA9F",  # Peanut butter blondies
    "72F50743-248E-4CC1-82E7-1B8D827D1FED",  # Peanut Butter Cookie Dough
]


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


async def og_image(session: aiohttp.ClientSession, url: str) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            print(f"  page status {r.status}")
            return None
        html = await r.text()
    m = re.search(
        r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html,
        re.I,
    ) or re.search(
        r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
        html,
        re.I,
    )
    return m.group(1) if m else None


async def main() -> None:
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        for uid in UIDS:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = body["result"]
            name = rec.get("name")
            src = (rec.get("source_url") or "").strip()
            print(f"=== {name}")
            print(f"  source_url: {src}")
            print(f"  photo: {rec.get('photo')}")
            if not src:
                print("  SKIP: no source_url")
                continue

            img_url = await og_image(s, src)
            print(f"  og:image: {img_url}")
            if not img_url:
                print("  SKIP: no og:image")
                continue

            async with s.get(img_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                raw = await r.read()
                print(f"  download: {r.status} {len(raw)} bytes")
                if r.status != 200 or len(raw) < 500:
                    print("  SKIP: bad download")
                    continue

            jpeg = to_jpeg(raw)
            photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
            rec["photo"] = photo_name
            rec["photo_large"] = photo_name
            rec["photo_hash"] = hashlib.sha256(jpeg).hexdigest().upper()
            rec["image_url"] = img_url
            rec["hash"] = calc_hash(rec)

            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            form.add_field(
                "photo_upload",
                jpeg,
                filename=photo_name,
                content_type="image/jpeg",
            )
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H, data=form
            ) as r:
                txt = await r.text()
                ok = '"result":true' in txt.replace(" ", "")
                print(f"  save: {r.status} ok={ok}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            print(f"notify: {r.status}")


if __name__ == "__main__":
    asyncio.run(main())
