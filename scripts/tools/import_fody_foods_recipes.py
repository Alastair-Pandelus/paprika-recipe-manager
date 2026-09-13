"""
Import FODY Foods low FODMAP recipes into Paprika under
Low Fodmap / FODY Foods.
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
FOLDER_NAME = "FODY Foods"
SITE = "https://www.fodyfoods.com"
BLOG_SITEMAP = f"{SITE}/sitemap_blogs_1.xml"
BLOG_PREFIX = f"{SITE}/blogs/low-fodmap-recipes/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaprikaImporter/1.0)"}


def safe_print(*args, **kwargs) -> None:
    try:
        print(*args, **kwargs, flush=True)
    except UnicodeEncodeError:
        print(
            *(str(a).encode("ascii", "replace").decode("ascii") for a in args),
            **kwargs,
            flush=True,
        )


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


def strip_tags(s: str | None) -> str:
    return clean_text(re.sub(r"<[^>]+>", "", s or ""))


def parse_iso_duration(iso: str | None) -> str:
    if not iso:
        return ""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return clean_text(iso)
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    parts = []
    if h:
        parts.append(f"{h} hr" if h == 1 else f"{h} hrs")
    if mi:
        parts.append(f"{mi} min" if mi == 1 else f"{mi} mins")
    if not parts and s:
        parts.append(f"{s} secs")
    return " ".join(parts)


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


def extract_ld_recipe(page_html: str) -> dict | None:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        re.I | re.S,
    )
    for block in blocks:
        # Shopify sometimes embeds raw newlines inside JSON strings
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            try:
                data = json.loads(block.replace("\r", "\\r").replace("\n", "\\n"))
            except json.JSONDecodeError:
                continue
        rec = find_recipe(data)
        if rec:
            return rec
    return None


def pick_image(img) -> str:
    if not img:
        return ""
    if isinstance(img, dict):
        return img.get("url") or img.get("contentUrl") or ""
    if isinstance(img, list):
        urls = []
        for item in img:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                urls.append(item.get("url") or item.get("contentUrl") or "")
        urls = [u for u in urls if u]
        return urls[0] if urls else ""
    if isinstance(img, str):
        return img
    return ""


def parse_ingredients_directions(page_html: str) -> tuple[list[str], list[str]]:
    heads: list[tuple[int, int, str]] = []
    for m in re.finditer(r"(?is)<h([23])[^>]*>(.*?)</h\1>", page_html):
        title = re.sub(r"\s+", " ", strip_tags(m.group(2))).strip()
        heads.append((m.start(), m.end(), title))

    ing_i = dir_i = None
    for i, (_s, _e, t) in enumerate(heads):
        tl = t.lower()
        if ing_i is None and "ingredient" in tl:
            ing_i = i
        if dir_i is None and (
            "direction" in tl or tl in {"method", "instructions"} or "instruction" in tl
        ):
            dir_i = i

    ings: list[str] = []
    dirs: list[str] = []

    if ing_i is not None:
        start = heads[ing_i][1]
        if dir_i is not None and dir_i > ing_i:
            end = heads[dir_i][0]
        elif ing_i + 1 < len(heads):
            end = heads[ing_i + 1][0]
        else:
            end = len(page_html)
        block = page_html[start:end]
        ings = [
            re.sub(r"\s+", " ", strip_tags(li)).strip()
            for li in re.findall(r"(?is)<li[^>]*>(.*?)</li>", block)
        ]
        ings = [x for x in ings if x]
        if not ings:
            ps = [
                re.sub(r"\s+", " ", strip_tags(p)).strip()
                for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", block)
            ]
            ings = [
                p
                for p in ps
                if p and not re.match(r"^(prep|cook|total|servings?|serves)\b", p, re.I)
            ]
        if not ings:
            text = re.sub(r"(?i)<br\s*/?>", "\n", block)
            text = strip_tags(re.sub(r"<[^>]+>", "\n", text))
            ings = [
                re.sub(r"\s+", " ", ln).strip()
                for ln in text.splitlines()
                if re.sub(r"\s+", " ", ln).strip()
            ]

    if dir_i is not None:
        start = heads[dir_i][1]
        end = heads[dir_i + 1][0] if dir_i + 1 < len(heads) else len(page_html)
        block = page_html[start:end]
        cut = re.search(
            r'(?is)<!--\s*Related|class="[^"]*share|id="comments|ArticleRating|'
            r"recently-viewed|newsletter|shopify-section",
            block,
        )
        if cut:
            block = block[: cut.start()]
        dirs = [
            re.sub(r"\s+", " ", strip_tags(p)).strip()
            for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", block)
        ]
        dirs = [p for p in dirs if p]
        if not dirs:
            text = re.sub(r"(?i)<br\s*/?>", "\n", block)
            text = strip_tags(re.sub(r"<[^>]+>", "\n", text))
            dirs = [
                re.sub(r"\s+", " ", ln).strip()
                for ln in text.splitlines()
                if re.sub(r"\s+", " ", ln).strip()
            ]

    return ings, dirs


def directions_to_text(dirs: list[str]) -> str:
    if not dirs:
        return ""
    lines = [f"{i}. {d}" for i, d in enumerate(dirs, 1)]
    return "\n".join(lines) + "\n"


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
        safe_print("Folder exists:", existing["uid"])
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
    safe_print("Created folder:", uid)
    return uid


async def list_recipe_urls(session: aiohttp.ClientSession) -> list[str]:
    async with session.get(BLOG_SITEMAP) as r:
        xml = await r.text()
        if r.status != 200:
            raise SystemExit(f"sitemap failed: {r.status}")
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    out = []
    seen = set()
    for loc in locs:
        loc = html_lib.unescape(loc).strip()
        if not loc.startswith(BLOG_PREFIX):
            continue
        # skip blog index
        if loc.rstrip("/") == BLOG_PREFIX.rstrip("/"):
            continue
        key = loc.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(loc)
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
        safe_print(f"  photo fail {img_url}: {ex}")
        return None


async def post_recipe(session, headers, recipe: dict, jpeg: bytes | None = None) -> bool:
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
            existing_urls: set[str] = set()
            existing_names: set[str] = set()

            catalog = await list_recipe_urls(web)
            safe_print(f"FODY LF blog posts listed: {len(catalog)}")

            ok_n = skip_n = fail_n = skip_not_recipe = 0
            for i, url in enumerate(catalog, 1):
                url_key = url.rstrip("/").lower()
                if url_key in existing_urls:
                    skip_n += 1
                    continue
                try:
                    async with web.get(url) as r:
                        page_html = await r.text()
                        if r.status != 200:
                            safe_print(f"[{i}/{len(catalog)}] FAIL status {r.status} {url}")
                            fail_n += 1
                            continue

                    ld = extract_ld_recipe(page_html)
                    ings, dirs = parse_ingredients_directions(page_html)
                    if not ings:
                        skip_not_recipe += 1
                        continue

                    name = clean_text((ld or {}).get("name") or "")
                    if not name:
                        m = re.search(
                            r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S
                        )
                        name = clean_text(m.group(1) if m else "")
                        name = re.sub(
                            r"\s*[\|\-–].*Fody.*$", "", name, flags=re.I
                        ).strip()
                    if not name:
                        name = url.rstrip("/").split("/")[-1].replace("-", " ").title()
                    if name.lower() in existing_names:
                        skip_n += 1
                        continue

                    ing_text = "\n".join(ings) + "\n"
                    directions = directions_to_text(dirs)
                    servings = (ld or {}).get("recipeYield")
                    if isinstance(servings, list):
                        servings = servings[0] if servings else ""
                    servings = clean_text(str(servings)) if servings is not None else ""

                    desc = clean_text((ld or {}).get("description") or "")
                    brief = ", ".join(x.split(",")[0][:40] for x in ings[:6])
                    description = f"Ingredients: {brief}\n\n{desc}" if brief else desc
                    notes = (
                        f"Source: FODY Foods — {url}\n\n"
                        "Imported for personal use. Check Monash app for current serve sizes."
                    )

                    img_url = pick_image((ld or {}).get("image"))
                    if not img_url:
                        m = re.search(
                            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                            page_html,
                            re.I,
                        )
                        if m:
                            img_url = m.group(1)
                    jpeg = (
                        await download_jpeg(web, urljoin(SITE, img_url)) if img_url else None
                    )

                    uid = str(uuid.uuid4()).upper()
                    recipe: dict = {
                        "uid": uid,
                        "name": name,
                        "ingredients": ing_text,
                        "directions": directions,
                        "description": description,
                        "notes": notes,
                        "servings": servings,
                        "prep_time": parse_iso_duration((ld or {}).get("prepTime")),
                        "cook_time": parse_iso_duration((ld or {}).get("cookTime")),
                        "total_time": parse_iso_duration((ld or {}).get("totalTime")),
                        "source": "FODY Foods",
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
                        safe_print(f"[{i}/{len(catalog)}] OK {name} photo={bool(jpeg)}")
                    else:
                        fail_n += 1
                        safe_print(f"[{i}/{len(catalog)}] FAIL post {name}")
                except Exception as ex:
                    fail_n += 1
                    safe_print(f"[{i}/{len(catalog)}] ERR {url}: {ex}")

                await asyncio.sleep(0.3)

            async with paprika.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=pheaders
            ) as r:
                await r.text()

            safe_print(
                f"\nDone. imported={ok_n} skipped_existing={skip_n} "
                f"skipped_not_recipe={skip_not_recipe} failed={fail_n} "
                f"listed={len(catalog)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
