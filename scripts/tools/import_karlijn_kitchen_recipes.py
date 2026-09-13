"""
Import Karlijn's Kitchen low FODMAP recipes into Paprika under
Low Fodmap / Karlijn's Kitchen.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html as html_lib
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
FOLDER_NAME = "Karlijn's Kitchen"
SITE = "https://www.karlijnskitchen.com"
RECIPES_CATEGORY = 1070
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaprikaImporter/1.0)"}


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


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return html_lib.unescape(s).replace("\xa0", " ").strip()


def parse_iso_duration(iso: str | None) -> str:
    if not iso:
        return ""
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
        text = clean_text(text)
        if not text:
            return
        if name:
            lines.append(f"{step_n}. {clean_text(name)}: {text}")
        else:
            lines.append(f"{step_n}. {text}")
        step_n += 1

    if isinstance(instr, str):
        return clean_text(instr)

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
                lines.append(f"\n{clean_text(section_name)}")
            for sub in item.get("itemListElement") or []:
                if isinstance(sub, dict):
                    add_step(sub.get("text") or "", sub.get("name"))
                elif isinstance(sub, str):
                    add_step(sub)
        else:
            add_step(item.get("text") or "", item.get("name"))
    return "\n".join(lines).strip() + ("\n" if lines else "")


def find_recipe(obj):
    if isinstance(obj, dict):
        typ = obj.get("@type")
        if typ == "Recipe" or (isinstance(typ, list) and "Recipe" in typ):
            return obj
        for g in obj.get("@graph") or []:
            found = find_recipe(g)
            if found:
                return found
        for v in obj.values():
            found = find_recipe(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_recipe(item)
            if found:
                return found
    return None


def extract_recipe_json(page_html: str) -> dict | None:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        re.I | re.S,
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        rec = find_recipe(data)
        if rec and (rec.get("recipeIngredient") or rec.get("recipeInstructions")):
            return rec
    return None


def is_low_fodmap(rec: dict, page_html: str) -> bool:
    blob = " ".join(
        [
            clean_text(rec.get("name")),
            clean_text(rec.get("description")),
            clean_text(str(rec.get("keywords") or "")),
            clean_text(str(rec.get("recipeCuisine") or "")),
            clean_text(str(rec.get("recipeCategory") or "")),
        ]
    ).lower()
    if "fodmap" in blob:
        return True
    # fall back to visible page copy (many GF-titled recipes still LF)
    return "fodmap" in page_html.lower()


def pick_image(img) -> str:
    if not img:
        return ""
    if isinstance(img, dict):
        return img.get("url") or ""
    if isinstance(img, list):
        urls = []
        for item in img:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and item.get("url"):
                urls.append(item["url"])
        if not urls:
            return ""
        # prefer largest / non-thumbnail
        def score(u: str) -> tuple:
            m = re.search(r"-(\d+)x(\d+)\.", u)
            if m:
                return (0, int(m.group(1)) * int(m.group(2)))
            return (1, len(u))

        return sorted(urls, key=score, reverse=True)[0]
    if isinstance(img, str):
        return img
    return ""


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


async def list_recipe_urls(session: aiohttp.ClientSession) -> list[str]:
    urls: list[str] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        api = (
            f"{SITE}/wp-json/wp/v2/posts"
            f"?categories={RECIPES_CATEGORY}&per_page=100&page={page}"
            f"&_fields=link,title"
        )
        async with session.get(api) as r:
            if r.status != 200:
                raise SystemExit(f"WP API failed page {page}: {r.status}")
            total_pages = int(r.headers.get("X-WP-TotalPages") or "1")
            data = await r.json()
        for post in data:
            link = (post.get("link") or "").strip()
            if "/en/" in link:
                urls.append(link)
        print(f"Listed page {page}/{total_pages} (+{len(data)})")
        page += 1
    # unique preserve order
    seen = set()
    out = []
    for u in urls:
        key = u.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
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

            async with paprika.get(
                f"{PAPRIKA_API}/v2/sync/recipes/", headers=pheaders
            ) as r:
                index = (await r.json())["result"]
            existing_urls: set[str] = set()
            existing_names: set[str] = set()
            sem = asyncio.Semaphore(4)

            async def fetch_recipe(uid: str) -> dict:
                for attempt in range(6):
                    async with sem:
                        async with paprika.get(
                            f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=pheaders
                        ) as r:
                            if r.status == 429:
                                await asyncio.sleep(1.5 * (attempt + 1))
                                continue
                            try:
                                payload = await r.json(content_type=None)
                            except Exception:
                                await asyncio.sleep(1.0 * (attempt + 1))
                                continue
                            return (payload or {}).get("result") or {}
                return {}

            async def scan_one(uid: str) -> None:
                rec = await fetch_recipe(uid)
                if not rec or rec.get("in_trash"):
                    return
                src = (rec.get("source_url") or "").rstrip("/").lower()
                cats = {str(c).lower() for c in (rec.get("categories") or [])}
                name = (rec.get("name") or "").lower()
                if "karlijnskitchen.com" in src or folder_uid.lower() in cats:
                    if src:
                        existing_urls.add(src)
                    if name:
                        existing_names.add(name)

            # Scan in batches to avoid Paprika 429s
            uids = [e["uid"] for e in index]
            for start in range(0, len(uids), 40):
                batch = uids[start : start + 40]
                await asyncio.gather(*(scan_one(u) for u in batch))
                await asyncio.sleep(0.5)
            print(
                f"Existing Karlijn imports: {len(existing_urls)} urls, "
                f"{len(existing_names)} names"
            )

            catalog = await list_recipe_urls(web)
            print(f"English recipe posts listed: {len(catalog)}")

            ok_n = skip_n = fail_n = skip_not_recipe = skip_not_lf = 0
            for i, url in enumerate(catalog, 1):
                url_key = url.rstrip("/").lower()
                if url_key in existing_urls:
                    skip_n += 1
                    continue
                try:
                    async with web.get(url) as r:
                        page_html = await r.text()
                        if r.status != 200:
                            print(f"[{i}/{len(catalog)}] FAIL status {r.status} {url}")
                            fail_n += 1
                            continue
                    data = extract_recipe_json(page_html)
                    if not data:
                        skip_not_recipe += 1
                        continue
                    if not is_low_fodmap(data, page_html):
                        skip_not_lf += 1
                        print(f"[{i}/{len(catalog)}] SKIP not LF {url}")
                        continue

                    name = clean_text(data.get("name") or "Untitled")
                    if name.lower() in existing_names:
                        skip_n += 1
                        continue

                    ingredients = data.get("recipeIngredient") or []
                    if isinstance(ingredients, str):
                        ing_text = clean_text(ingredients)
                    else:
                        ing_text = (
                            "\n".join(clean_text(str(x)) for x in ingredients) + "\n"
                        )

                    directions = instructions_to_text(data.get("recipeInstructions"))
                    servings = data.get("recipeYield")
                    if isinstance(servings, list):
                        servings = servings[0] if servings else ""
                    servings = str(servings) if servings is not None else ""

                    desc = clean_text(data.get("description") or "")
                    brief = ", ".join(
                        clean_text(str(x)).split(",")[0][:40]
                        for x in (ingredients[:6] or [])
                    )
                    description = (
                        f"Ingredients: {brief}\n\n{desc}" if brief else desc
                    )
                    notes = (
                        f"Source: Karlijn's Kitchen — {url}\n\n"
                        "Imported for personal use. Check Monash app for current serve sizes."
                    )

                    img_url = pick_image(data.get("image"))
                    if not img_url:
                        m = re.search(
                            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                            page_html,
                            re.I,
                        )
                        if m:
                            img_url = m.group(1)
                    jpeg = await download_jpeg(web, urljoin(SITE, img_url)) if img_url else None

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
                        "source": "Karlijn's Kitchen",
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
                        print(f"[{i}/{len(catalog)}] OK {name} photo={bool(jpeg)}")
                    else:
                        fail_n += 1
                        print(f"[{i}/{len(catalog)}] FAIL post {name}")
                except Exception as ex:
                    fail_n += 1
                    print(f"[{i}/{len(catalog)}] ERR {url}: {ex}")

                await asyncio.sleep(0.3)

            async with paprika.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=pheaders
            ) as r:
                await r.text()

            print(
                f"\nDone. imported={ok_n} skipped_existing={skip_n} "
                f"skipped_not_recipe={skip_not_recipe} skipped_not_lf={skip_not_lf} "
                f"failed={fail_n} listed={len(catalog)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
