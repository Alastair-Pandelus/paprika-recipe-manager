"""Upload local JPEGs as photos for the two Karlijn recipes missing images."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
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

UPLOADS = [
    (
        "3EFE6939-7E62-480A-BBFF-C1896E8AEA9F",
        Path(r"C:\Users\Pandelus\.cursor\projects\c-Cursor-Chat\assets\peanut-butter-blondies.jpg"),
    ),
    (
        "72F50743-248E-4CC1-82E7-1B8D827D1FED",
        Path(
            r"C:\Users\Pandelus\.cursor\projects\c-Cursor-Chat\assets\peanut-butter-cookie-dough-brownies.jpg"
        ),
    ),
]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def to_jpeg(path: Path) -> bytes:
    im = Image.open(path).convert("RGB")
    im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


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

        for uid, path in UPLOADS:
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = body["result"]
            print(f"=== {rec.get('name')}")
            jpeg = to_jpeg(path)
            photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
            rec["photo"] = photo_name
            rec["photo_large"] = photo_name
            rec["photo_hash"] = hashlib.sha256(jpeg).hexdigest().upper()
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
                print(f"  save: {r.status} ok={ok} photo={photo_name} bytes={len(jpeg)}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            print(f"notify: {r.status}")


if __name__ == "__main__":
    asyncio.run(main())
