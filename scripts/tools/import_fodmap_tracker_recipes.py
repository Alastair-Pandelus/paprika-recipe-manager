"""
Import all FODMAP Tracker recipes into Paprika under
Low Fodmap / Fodmap Tracker App, named e.g. 'Soup - Chicken Broth'.
"""
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
from urllib.parse import urljoin

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
FOLDER_NAME = "Fodmap Tracker App"
SITE = "https://fodmaptracker.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaprikaImporter/1.0)"}

# Section page path -> Paprika name prefix (singular, as requested)
SECTIONS: list[tuple[str, str]] = [
    ("/recipes/breakfast/", "Breakfast"),
    ("/recipes/meals/", "Meal"),
    ("/recipes/sides/", "Side"),
    ("/recipes/soups/", "Soup"),
    ("/recipes/snacks/", "Snack"),
    ("/recipes/sweets/", "Sweet"),
    ("/recipes/sauces-condiments/", "Sauce"),
    ("/recipes/smoothies/", "Smoothie"),
    ("/recipes/mocktails/", "Mocktail"),
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
    im.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def parse_iso_duration(iso: str | None) -> str:
    if not iso:
        return ""
    # PT1H15M / PT30M / PT6M
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return iso
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    parts = []
    if h:
        parts.append(f"{h} hr" if h == 1 else f"{h} hrs")
    if mi:
        parts.append(f"{mi} min" if mi == 1 else f"{mi} mins")
    if not parts and s:
        parts.append(f"{s} secs")
    return " ".join(parts)


def instructions_to_text(instr) -> str:
    if not instr:
        return ""
    lines: list[str] = []
    step_n = 1

    def add_step(text: str, name: str | None = None) -> None:
        nonlocal step_n
        text = (text or "").strip()
        if not text:
            return
        if name:
            lines.append(f"{step_n}. {name}: {text}")
        else:
            lines.append(f"{step_n}. {text}")
        step_n += 1

    if isinstance(instr, str):
        return instr.strip()

    for item in instr:
        if isinstance(item, str):
            add_step(item)
            continue
        if not isinstance(item, dict):
            continue
        typ = item.get("@type")
        if typ == "HowToSection" or "itemListElement" in item:
            section_name = item.get("name")
            if section_name:
                lines.append(f"\n{section_name}")
            for sub in item.get("itemListElement") or []:
                if isinstance(sub, dict):
                    add_step(sub.get("text") or "", sub.get("name"))
                elif isinstance(sub, str):
                    add_step(sub)
        else:
            add_step(item.get("text") or "", item.get("name"))
    return "\n".join(lines).strip() + ("\n" if lines else "")


def extract_recipe_json(html: str) -> dict | None:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") == "Recipe":
                return obj
            # @graph
            for g in obj.get("@graph") or []:
                if isinstance(g, dict) and g.get("@type") == "Recipe":
                    return g
    return None


async def ensure_folder(session, headers) -> str:
    async with session.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
        cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
    existing = next(
        (
            c
            for c in cats
            if (c.get("name") or "") == FOLDER_NAME
            and (c.get("parent_uid") or "").lower() == LF.lower()
        ),
        None,
    )
    if existing:
        print("Folder exists:", existing["uid"])
        return existing["uid"]
    uid = str(uuid.uuid4()).upper()
    item = {"uid": uid, "name": FOLDER_NAME, "parent_uid": LF, "order_flag": 0}
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj([item]), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/categories/", headers=headers, data=form
    ) as r:
        body = await r.text()
        if '"result":true' not in body.replace(" ", ""):
            raise SystemExit(f"folder create failed: {body[:300]}")
    print("Created folder:", uid)
    return uid


async def list_section_recipes(
    session: aiohttp.ClientSession, section_path: str
) -> list[str]:
    url = urljoin(SITE, section_path)
    async with session.get(url) as r:
        html = await r.text()
        if r.status != 200:
            print(f"  WARN section {url} status {r.status}")
            return []
    hrefs = re.findall(r'href=["\'](/recipes/low-fodmap-[^"\']+/?)["\']', html)
    # unique preserve order
    seen = set()
    out = []
    for h in hrefs:
        if h in seen:
            continue
        seen.add(h)
        out.append(urljoin(SITE, h))
    return out


async def download_jpeg(session: aiohttp.ClientSession, img_url: str) -> bytes | None:
    if not img_url:
        return None
    try:
        async with session.get(img_url) as r:
            if r.status != 200:
                return None
            raw = await r.read()
        if len(raw) < 500:
            return None
        return to_jpeg(raw)
    except Exception as ex:
        print(f"  photo fail {img_url}: {ex}")
        return None


async def post_recipe(session, headers, recipe: dict, jpeg: bytes | None) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
    )
    if jpeg and recipe.get("photo"):
        form.add_field(
            "photo_upload",
            jpeg,
            filename=recipe["photo"],
            content_type="image/jpeg",
        )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


async def main() -> None:
    user, password = paprika_credentials()
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as web:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as paprika:
            async with paprika.post(
                f"{PAPRIKA_API}/v1/account/login",
                data={"email": user, "password": password},
            ) as r:
                token = (await r.json())["result"]["token"]
            pheaders = {"Authorization": f"Bearer {token}"}

            folder_uid = await ensure_folder(paprika, pheaders)

            # Skip recipes already imported from this site / already in this folder
            async with paprika.get(
                f"{PAPRIKA_API}/v2/sync/recipes/", headers=pheaders
            ) as r:
                index = (await r.json())["result"]
            existing_urls: set[str] = set()
            existing_names: set[str] = set()
            sem = asyncio.Semaphore(20)

            async def scan_one(uid: str) -> None:
                async with sem:
                    async with paprika.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=pheaders
                    ) as r:
                        rec = (await r.json()).get("result") or {}
                if rec.get("in_trash"):
                    return
                src = (rec.get("source_url") or "").rstrip("/").lower()
                cats = {str(c).lower() for c in (rec.get("categories") or [])}
                name = (rec.get("name") or "").lower()
                if "fodmaptracker.com" in src or folder_uid.lower() in cats:
                    if src:
                        existing_urls.add(src)
                    if name:
                        existing_names.add(name)

            await asyncio.gather(*(scan_one(e["uid"]) for e in index))
            print(
                f"Existing FODMAP Tracker imports: {len(existing_urls)} urls, "
                f"{len(existing_names)} names"
            )

            # Collect all section recipes
            catalog: list[tuple[str, str]] = []  # (prefix, url)
            for path, prefix in SECTIONS:
                urls = await list_section_recipes(web, path)
                print(f"{prefix}: {len(urls)} recipes from {path}")
                for u in urls:
                    catalog.append((prefix, u))

            # de-dupe by URL (keep first section assignment)
            seen_u = set()
            unique: list[tuple[str, str]] = []
            for prefix, u in catalog:
                key = u.rstrip("/").lower()
                if key in seen_u:
                    continue
                seen_u.add(key)
                unique.append((prefix, u))
            print(f"Unique recipes to process: {len(unique)}")

            ok_n = skip_n = fail_n = 0
            for i, (prefix, url) in enumerate(unique, 1):
                url_key = url.rstrip("/").lower()
                if url_key in existing_urls:
                    skip_n += 1
                    continue

                try:
                    async with web.get(url) as r:
                        html = await r.text()
                        if r.status != 200:
                            print(f"[{i}/{len(unique)}] FAIL status {r.status} {url}")
                            fail_n += 1
                            continue
                    data = extract_recipe_json(html)
                    if not data:
                        print(f"[{i}/{len(unique)}] FAIL no ld+json {url}")
                        fail_n += 1
                        continue

                    bare = (data.get("name") or "Untitled").strip()
                    name = f"{prefix} - {bare}"
                    if name.lower() in existing_names:
                        skip_n += 1
                        continue

                    ingredients = data.get("recipeIngredient") or []
                    if isinstance(ingredients, str):
                        ing_text = ingredients
                    else:
                        ing_text = "\n".join(str(x) for x in ingredients) + "\n"

                    directions = instructions_to_text(data.get("recipeInstructions"))
                    servings = data.get("recipeYield")
                    if isinstance(servings, list):
                        servings = servings[0] if servings else ""
                    servings = str(servings) if servings is not None else ""

                    desc = data.get("description") or ""
                    # brief ingredients line
                    brief = ", ".join(
                        (str(x).split(",")[0])[:40] for x in (ingredients[:6] or [])
                    )
                    description = (
                        f"Ingredients: {brief}\n\n{desc}" if brief else desc
                    )

                    notes = (
                        f"Source: FODMAP Tracker — {url}\n\n"
                        "Imported for personal use. Check Monash app for current serve sizes."
                    )

                    img = data.get("image")
                    if isinstance(img, list):
                        img = img[0] if img else ""
                    if isinstance(img, dict):
                        img = img.get("url") or ""
                    jpeg = await download_jpeg(web, img) if img else None

                    uid = str(uuid.uuid4()).upper()
                    recipe: dict = {
                        "uid": uid,
                        "name": name,
                        "ingredients": ing_text,
                        "directions": directions,
                        "description": description,
                        "notes": notes,
                        "servings": servings,
                        "prep_time": parse_iso_duration(data.get("prepTime")),
                        "cook_time": parse_iso_duration(data.get("cookTime")),
                        "total_time": parse_iso_duration(data.get("totalTime")),
                        "source": "FODMAP Tracker",
                        "source_url": url,
                        "categories": [folder_uid],
                        "rating": 0,
                        "difficulty": "",
                        "nutrition": "",
                        "photo": None,
                        "photo_hash": None,
                        "photo_large": None,
                        "photo_url": None,
                        "image_url": None,
                        "in_trash": False,
                        "is_pinned": False,
                        "on_favorites": False,
                        "on_grocery_list": False,
                        "scale": None,
                        "hash": None,
                        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if jpeg:
                        photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
                        recipe["photo"] = photo_name
                        recipe["photo_large"] = photo_name
                        recipe["photo_hash"] = hashlib.sha256(jpeg).hexdigest().upper()

                    recipe["hash"] = calc_hash(recipe)
                    success = await post_recipe(paprika, pheaders, recipe, jpeg)
                    if success:
                        ok_n += 1
                        existing_urls.add(url_key)
                        existing_names.add(name.lower())
                        print(
                            f"[{i}/{len(unique)}] OK {name} photo={bool(jpeg)}"
                        )
                    else:
                        fail_n += 1
                        print(f"[{i}/{len(unique)}] FAIL post {name}")
                except Exception as ex:
                    fail_n += 1
                    print(f"[{i}/{len(unique)}] ERR {url}: {ex}")

                # gentle pacing
                await asyncio.sleep(0.35)

            async with paprika.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=pheaders
            ) as r:
                await r.text()

            print(
                f"\nDone. imported={ok_n} skipped={skip_n} failed={fail_n} total={len(unique)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
