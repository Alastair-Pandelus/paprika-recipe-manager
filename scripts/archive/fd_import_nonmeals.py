"""Import Field Doctor Low FODMAP porridge + bars into Paprika (8 portions/bars)."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html as html_lib
import io
import json
import os
import re
import sys
import types
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
from PIL import Image

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
COLL = "https://www.fielddoctor.co.uk/collections/low-fodmap/products.json?limit=250"
PORTIONS = 8
RESCALE = Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_rescale_density.py")

HANDLES = [
    # porridge
    "porridge-classic",
    "porridge-cinnamon",
    "porridge-cherry-chocolate",
    "no-sugar-banana-porridge",
    # bars
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


def load_rescale():
    mod = types.ModuleType("fd_rescale")
    sys.modules["fd_rescale"] = mod
    exec(compile(RESCALE.read_text(encoding="utf-8"), str(RESCALE), "exec"), mod.__dict__)
    return mod


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def extract_ingredient_list(html: str) -> str | None:
    key = 'ingredient_list\\",\\"'
    i = html.find(key)
    if i < 0:
        return None
    start = i + len(key)
    end = html.find('\\",\\"', start)
    raw = html[start:end] if end > start else html[start : start + 4000]
    try:
        text = raw.encode("utf-8").decode("unicode_escape")
    except Exception:
        text = raw
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("Enzymes}", "Enzymes]").replace("Water}", "Water]")
    text = re.split(r"(?i)for allergens|manufactured on a site", text)[0]
    return re.sub(r"\s+", " ", text).strip(" ,.")


def extract_serving_g(html: str, kind: str) -> int:
    servings = [int(x) for x in re.findall(r"per (\d{2,4})g", html, re.I)]
    non100 = [s for s in servings if s != 100]
    if non100:
        return max(non100)
    if servings:
        return max(servings)
    return 50 if kind == "bar" else 55


def kind_for(handle: str, title: str) -> str:
    t = f"{handle} {title}".lower()
    if "porridge" in t:
        return "porridge"
    return "bar"


def recipe_title(shop_title: str, kind: str) -> str:
    base = re.sub(r"\s*\(Low FODMAP\)\s*", "", shop_title, flags=re.I).strip()
    if kind == "porridge" and "porridge" not in base.lower():
        base = f"{base} Porridge"
    if kind == "bar" and "bar" not in base.lower():
        base = f"{base} Bar"
    return f"Field Doctor-Style {base} (Low FODMAP)"


def directions_for(kind: str, title: str) -> str:
    if kind == "porridge":
        return "\n\n".join(
            [
                "1. Dry mix: Stir together all dry ingredients until evenly combined.",
                "2. Cook (per portion): Use about 1/8 of the dry mix. Cook with Low FODMAP milk alternative "
                "(e.g. lactose-free milk or suitable plant milk) or water, using roughly 150–200 ml liquid "
                "per portion, until creamy. Adjust thickness to taste.",
                "3. Finish: Sweeten further only if needed. Cool leftovers; refrigerate up to 3 days. "
                "Reheat with a splash of liquid.",
            ]
        )
    # bars — no-bake style recreation of Fodbods-style bars sold via Field Doctor
    return "\n\n".join(
        [
            "1. Prep: Line a small tray/tin (for 8 bars) with baking paper.",
            "2. Mix: Process nuts/seeds if needed, then mix with syrup/binder, protein blend, flavourings "
            "and any chocolate/fruit pieces until a sticky dough forms. Warm the syrup slightly if the mix "
            "is too stiff.",
            "3. Press: Firmly press into the tin in an even layer. Chill until set (about 1–2 hours, or overnight).",
            "4. Cut: Cut into 8 bars. Keep refrigerated up to a week, or freeze. "
            f"Inspired by {title} — gluten-free / Low FODMAP friendly recreation.",
        ]
    )


def notes_preamble(kind: str) -> str:
    if kind == "porridge":
        return (
            "REVERSE-ENGINEERED from Field Doctor published ingredients for personal home cooking only. "
            "Official product is Monash Low FODMAP certified at single-serve size. "
            "This is a dry porridge mix — cook with Low FODMAP liquid as directed."
        )
    return (
        "REVERSE-ENGINEERED from Field Doctor / Fodbods published ingredients for personal home cooking only. "
        "Official bar is Monash Low FODMAP certified at single-serve size. No-bake style recreation."
    )


async def fetch_photo_jpeg(session: aiohttp.ClientSession, url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                return None
            data = await r.read()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1200, 1200))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        print("photo fail", e)
        return None


async def main() -> None:
    load_env()
    fd = load_rescale()
    user = os.environ["PAPRIKA_USERNAME"]
    password = os.environ["PAPRIKA_PASSWORD"]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={"email": user, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # existing FD recipes + a template payload
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]
        existing_urls = set()
        template = None
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            existing_urls.add(recipe.get("source_url") or "")
            if template is None:
                template = recipe
            # delete accidental test recipe
            if (recipe.get("name") or "").startswith("TEMP FD Import Test"):
                trash = dict(recipe)
                trash["in_trash"] = True
                trash["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                trash["hash"] = calc_hash(trash)
                form = aiohttp.FormData()
                form.add_field(
                    "data",
                    gzip_obj(trash),
                    content_type="application/octet-stream",
                    filename="data",
                )
                async with session.post(
                    f"{BASE}/v2/sync/recipe/{trash['uid']}/", headers=headers, data=form
                ) as r:
                    await r.text()
                print("trashed test recipe")

        if not template:
            raise SystemExit("No Field Doctor template recipe found")

        async with session.get(COLL, headers={"User-Agent": "Mozilla/5.0"}) as r:
            products = (await r.json())["products"]
        by_handle = {p["handle"]: p for p in products}

        created = 0
        for handle in HANDLES:
            p = by_handle.get(handle)
            if not p:
                print("MISSING", handle)
                continue
            source_url = f"https://www.fielddoctor.co.uk/products/{handle}"
            if source_url in existing_urls:
                print("SKIP exists", handle)
                continue

            title = p.get("title") or handle
            kind = kind_for(handle, title)
            async with session.get(source_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            raw = extract_ingredient_list(html)
            if not raw:
                print("SKIP no ingredients", handle)
                continue
            serving = extract_serving_g(html, kind)

            items = fd.merge_duplicate_ingredients(
                fd.allocate_percentages(fd.parse_ingredients(raw))
            )
            omitted_water = next((it for it in items if fd.is_plain_water(it.name)), None)
            items = [it for it in items if not fd.is_plain_water(it.name)]
            omitted_oil = None
            rname = recipe_title(title, kind)
            rebuilt = []
            for it in items:
                if not fd.is_olive_oil(it.name):
                    rebuilt.append(it)
                    continue
                if fd.should_keep_olive_oil(
                    it, serving_g=serving, portions=PORTIONS, recipe_name=rname
                ):
                    rebuilt.append(it)
                else:
                    omitted_oil = it
            items = rebuilt

            ings_text = fd.build_ingredient_text(items, serving, PORTIONS)
            scaling = fd.build_scaling_notes(
                items,
                serving,
                PORTIONS,
                omitted_water=omitted_water,
                omitted_oil=omitted_oil,
            )
            notes = fd.merge_recipe_notes(notes_preamble(kind), scaling, raw)

            photo_url = ""
            if p.get("images"):
                photo_url = p["images"][0].get("src") or ""
            photo_bytes = await fetch_photo_jpeg(session, photo_url)

            uid = str(uuid.uuid4()).upper()
            recipe = {k: template[k] for k in template}
            # Wipe media fields from template so we don't inherit broken thumbnails
            for k in (
                "photo",
                "photo_hash",
                "photo_large",
                "photo_url",
                "image_url",
                "photos",
            ):
                if k in recipe:
                    recipe[k] = None if k != "photo_url" else ""
            recipe.update(
                {
                    "uid": uid,
                    "name": rname,
                    "ingredients": ings_text,
                    "directions": directions_for(kind, title),
                    "notes": notes,
                    "servings": str(PORTIONS),
                    "servings_min": None,
                    "servings_max": None,
                    "source": "Field Doctor",
                    "source_url": source_url,
                    "categories": [FD, LF],
                    "photo": None,
                    "photo_hash": None,
                    "photo_large": None,
                    "photo_url": "",
                    "image_url": None,
                    "scale": None,
                    "rating": 0,
                    "difficulty": "easy",
                    "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "prep_time": "5" if kind == "porridge" else "15",
                    "cook_time": "10" if kind == "porridge" else "0",
                    "total_time": "",
                    "prep_minutes": 5 if kind == "porridge" else 15,
                    "cook_minutes": 10 if kind == "porridge" else 0,
                    "total_minutes": None,
                    "nutritional_info": "",
                    "description": f"Inspired recreation of Field Doctor {title} for home cooking.",
                    "in_trash": False,
                    "is_pinned": False,
                    "on_favorites": False,
                    "on_grocery_list": False,
                    "cookbook_uid": None,
                }
            )
            recipe["hash"] = calc_hash(recipe)

            # Create without photo first (avoids thumbnail filename errors), then attach photo
            form = aiohttp.FormData()
            form.add_field(
                "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
            )
            async with session.post(
                f"{BASE}/v2/sync/recipe/{uid}/", headers=headers, data=form
            ) as r:
                body = await r.text()
                ok = '"result":true' in body.replace(" ", "")
            if ok and photo_bytes:
                async with session.get(
                    f"{BASE}/v2/sync/recipe/{uid}/", headers=headers
                ) as r:
                    recipe = (await r.json()).get("result") or recipe
                recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                recipe["hash"] = calc_hash(recipe)
                form2 = aiohttp.FormData()
                form2.add_field(
                    "data",
                    gzip_obj(recipe),
                    content_type="application/octet-stream",
                    filename="data",
                )
                form2.add_field(
                    "photo_upload",
                    photo_bytes,
                    content_type="image/jpeg",
                    filename="photo.jpg",
                )
                async with session.post(
                    f"{BASE}/v2/sync/recipe/{uid}/", headers=headers, data=form2
                ) as r:
                    photo_ok = '"result":true' in (await r.text()).replace(" ", "")
                if not photo_ok:
                    print("  photo attach failed (recipe created)")

            print(
                ("CREATED" if ok else "FAIL"),
                rname,
                f"serving={serving}g",
                f"n={len(items)}",
                "" if ok else body[:220],
            )
            if ok:
                created += 1
                existing_urls.add(source_url)

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("created", created)


if __name__ == "__main__":
    asyncio.run(main())
