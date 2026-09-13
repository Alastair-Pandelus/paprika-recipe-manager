"""Fix Monash recipe name prefixes; add photo for Tuna & Sweet Potato Patties."""
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
from urllib.parse import quote

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

MONASH = "1F399917-8D6D-4AA4-A7EB-60C75EC4EB08"


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


def ensure_monash_prefix(name: str) -> str:
    n = (name or "").strip()
    if n.lower().startswith("monash - "):
        return n
    if n.lower().startswith("monash-"):
        return "Monash - " + n[7:].lstrip(" -")
    if n.lower().startswith("monash "):
        return "Monash - " + n[6:].lstrip(" -")
    return f"Monash - {n}"


async def find_monash_image(session: aiohttp.ClientSession, source_url: str, name: str) -> bytes | None:
    """Try recipe page og:image, else Monash search heuristics."""
    headers = {"User-Agent": "Mozilla/5.0"}
    urls_to_try = []
    if source_url and "monashfodmap.com" in source_url.lower():
        urls_to_try.append(source_url)
    # slug from name without prefix
    bare = re.sub(r"(?i)^monash\s*-\s*", "", name).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", bare.lower()).strip("-")
    if slug:
        urls_to_try.append(f"https://monashfodmap.com/recipe/{slug}/")

    for page in urls_to_try:
        try:
            async with session.get(page, headers=headers) as r:
                if r.status != 200:
                    continue
                html = await r.text()
            m = re.search(
                r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                html,
                re.I,
            ) or re.search(
                r'content=["\'](https://fodmap-publicsite[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
                html,
                re.I,
            )
            if not m:
                # original image if fill version found
                imgs = re.findall(
                    r"https://fodmap-publicsite[^\"'\s>]+\.(?:jpg|jpeg|png|webp)",
                    html,
                    re.I,
                )
                if not imgs:
                    continue
                img_url = imgs[0]
                for u in imgs:
                    if "original" in u:
                        img_url = u
                        break
            else:
                img_url = m.group(1)
                # prefer original
                orig = img_url.replace(".fill-", ".original.").rsplit(".", 1)
                # try swapping fill path for original
                cand = re.sub(r"\.[^/]*fill-[^/]+\.", ".original.", img_url)
                if cand != img_url:
                    img_url = cand
            async with session.get(img_url, headers=headers) as r:
                if r.status == 200:
                    raw = await r.read()
                    if len(raw) > 1000:
                        print(f"  image from {img_url[:80]}... ({len(raw)} bytes)")
                        return raw
        except Exception as ex:
            print(f"  image fetch fail {page}: {ex}")
    return None


async def upload_photo(session, headers, recipe: dict, jpeg: bytes) -> bool:
    photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
    photo_hash = hashlib.sha256(jpeg).hexdigest().upper()
    recipe = dict(recipe)
    recipe["photo"] = photo_name
    recipe["photo_large"] = photo_name
    recipe["photo_hash"] = photo_hash
    recipe["photo_url"] = None
    recipe["image_url"] = None
    recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recipe["hash"] = calc_hash(recipe)
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
    )
    form.add_field(
        "photo_upload", jpeg, filename=photo_name, content_type="image/jpeg"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        monash_recipes = []
        tuna = None
        for entry in index:
            async with session.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                rec = (await r.json()).get("result") or {}
            if rec.get("in_trash"):
                continue
            cats = [c.lower() for c in (rec.get("categories") or [])]
            name = rec.get("name") or ""
            if MONASH.lower() in cats or name.lower().startswith("monash"):
                monash_recipes.append(rec)
            if "tuna" in name.lower() and "sweet potato" in name.lower() and "patti" in name.lower():
                tuna = rec
            elif "tuna" in name.lower() and "sweet potato" in name.lower():
                tuna = rec

        print(f"Monash-related recipes: {len(monash_recipes)}")
        renames = []
        for rec in monash_recipes:
            old = rec.get("name") or ""
            new = ensure_monash_prefix(old)
            # also ensure only Monash category
            cats = list(rec.get("categories") or [])
            if MONASH not in cats and MONASH.lower() not in [c.lower() for c in cats]:
                cats = [MONASH]
            else:
                cats = [MONASH]  # Low Fodmap only under Monash leaf
            changed = new != old or [c.lower() for c in (rec.get("categories") or [])] != [
                MONASH.lower()
            ]
            if changed:
                print(f"  RENAME: {old!r} -> {new!r}")
                rec["name"] = new
                rec["categories"] = [MONASH]
                # keep monash source if missing and URL looks monash
                if not rec.get("source"):
                    rec["source"] = "Monash FODMAP"
                rec["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rec["hash"] = calc_hash(rec)
                form = aiohttp.FormData()
                form.add_field(
                    "data",
                    gzip_obj(rec),
                    content_type="application/octet-stream",
                    filename="data",
                )
                async with session.post(
                    f"{PAPRIKA_API}/v2/sync/recipe/{rec['uid']}/",
                    headers=headers,
                    data=form,
                ) as r:
                    ok = '"result":true' in (await r.text()).replace(" ", "")
                renames.append((old, new, ok))
            else:
                print(f"  OK already: {old}")

        print(f"Renamed {sum(1 for _,_,ok in renames if ok)}/{len(renames)}")

        # Tuna photo
        if not tuna:
            # search again after renames
            for rec in monash_recipes:
                n = (rec.get("name") or "").lower()
                if "tuna" in n and "sweet" in n:
                    tuna = rec
                    break
        if not tuna:
            raise SystemExit("Tuna & Sweet Potato Patties not found")

        print("Tuna recipe:", tuna.get("name"), tuna.get("uid"))
        print("  current photo:", tuna.get("photo"), "url:", bool(tuna.get("photo_url")))
        raw = await find_monash_image(
            session, tuna.get("source_url") or "", tuna.get("name") or ""
        )
        if not raw:
            # try known slug variants
            for slug in (
                "tuna-sweet-potato-patties",
                "tuna-and-sweet-potato-patties",
                "sweet-potato-tuna-patties",
            ):
                raw = await find_monash_image(
                    session, f"https://monashfodmap.com/recipe/{slug}/", tuna.get("name") or ""
                )
                if raw:
                    break
        if not raw:
            raise SystemExit("Could not find Monash image for tuna patties")
        jpeg = to_jpeg(raw)
        # refresh recipe before upload
        async with session.get(
            f"{PAPRIKA_API}/v2/sync/recipe/{tuna['uid']}/", headers=headers
        ) as r:
            tuna = (await r.json())["result"]
        # ensure name prefix on tuna too
        tuna["name"] = ensure_monash_prefix(tuna.get("name") or "")
        tuna["categories"] = [MONASH]
        if not tuna.get("source"):
            tuna["source"] = "Monash FODMAP"
        ok = await upload_photo(session, headers, tuna, jpeg)
        print("Photo upload:", ok)

        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        async with session.get(
            f"{PAPRIKA_API}/v2/sync/recipe/{tuna['uid']}/", headers=headers
        ) as r:
            t2 = (await r.json())["result"]
        print("Final tuna:", t2.get("name"), "photo_url=", bool(t2.get("photo_url")))
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
