"""Create Original Porridge + attach photos to all non-meals; reparse peanut bar."""
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


def load_rescale():
    mod = types.ModuleType("fd_rescale2")
    sys.modules["fd_rescale2"] = mod
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
    text = re.split(r"(?i)for allergens|manufactured on a site", text)[0]
    text = text.replace("\\t", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", text).strip(" ,.")


def extract_serving_g(html: str, kind: str) -> int:
    servings = [int(x) for x in re.findall(r"per (\d{2,4})g", html, re.I)]
    non100 = [s for s in servings if s != 100]
    if non100:
        return max(non100)
    return 50 if kind == "bar" else 55


def kind_for(handle: str, title: str) -> str:
    return "porridge" if "porridge" in f"{handle} {title}".lower() else "bar"


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
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
        if r.status != 200:
            return None
        data = await r.read()
    im = Image.open(io.BytesIO(data)).convert("RGB")
    # Paprika rejects very large PNGs; keep JPEG modest
    im.thumbnail((1000, 1000))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


async def main() -> None:
    load_env()
    fd = load_rescale()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]
        by_url = {}
        template = None
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            if template is None and recipe.get("photo"):
                template = recipe
            by_url[recipe.get("source_url") or ""] = recipe
        if template is None:
            # any FD recipe
            for recipe in by_url.values():
                template = recipe
                break

        async with session.get(COLL, headers={"User-Agent": "Mozilla/5.0"}) as r:
            products = {p["handle"]: p for p in (await r.json())["products"]}

        for handle in HANDLES:
            p = products[handle]
            title = p["title"]
            kind = kind_for(handle, title)
            source_url = f"https://www.fielddoctor.co.uk/products/{handle}"
            async with session.get(source_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            raw = extract_ingredient_list(html)
            serving = extract_serving_g(html, kind)
            items = fd.merge_duplicate_ingredients(
                fd.allocate_percentages(fd.parse_ingredients(raw))
            )
            items = [it for it in items if not fd.is_plain_water(it.name)]
            ings = fd.build_ingredient_text(items, serving, PORTIONS)
            scaling = fd.build_scaling_notes(items, serving, PORTIONS)
            notes = fd.merge_recipe_notes(notes_preamble(kind), scaling, raw)
            clean_title = re.sub(r"\s*\(Low FODMAP\)\s*", "", title, flags=re.I).strip()
            rname = f"Field Doctor-Style {clean_title} (Low FODMAP)"

            recipe = by_url.get(source_url)
            created_new = False
            if not recipe:
                created_new = True
                uid = str(uuid.uuid4()).upper()
                recipe = {k: template[k] for k in template}
                for k in ("photo", "photo_hash", "photo_large", "image_url"):
                    recipe[k] = None
                recipe["photo_url"] = ""
                recipe["uid"] = uid

            recipe["name"] = rname
            recipe["ingredients"] = ings
            recipe["directions"] = directions_for(kind, title)
            recipe["notes"] = notes
            recipe["servings"] = str(PORTIONS)
            recipe["categories"] = [FD, LF]
            recipe["source"] = "Field Doctor"
            recipe["source_url"] = source_url
            recipe["description"] = f"Inspired recreation of Field Doctor {title}."
            recipe["in_trash"] = False
            recipe["prep_time"] = "5" if kind == "porridge" else "15"
            recipe["cook_time"] = "10" if kind == "porridge" else "0"
            recipe["prep_minutes"] = 5 if kind == "porridge" else 15
            recipe["cook_minutes"] = 10 if kind == "porridge" else 0
            # clear thumbs before photo upload
            recipe["photo"] = None
            recipe["photo_hash"] = None
            recipe["photo_large"] = None
            recipe["photo_url"] = ""
            recipe["image_url"] = None
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe["hash"] = calc_hash(recipe)

            form = aiohttp.FormData()
            form.add_field(
                "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
            )
            async with session.post(
                f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            if not ok:
                print("FAIL save", handle)
                continue

            # reload then photo
            async with session.get(
                f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json())["result"]
            photo_url = (p.get("images") or [{}])[0].get("src") or ""
            photo_bytes = await fetch_photo_jpeg(session, photo_url)
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe["hash"] = calc_hash(recipe)
            form2 = aiohttp.FormData()
            form2.add_field(
                "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
            )
            form2.add_field(
                "photo_upload", photo_bytes, filename="recipe.jpg", content_type="image/jpeg"
            )
            async with session.post(
                f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form2
            ) as r:
                body = await r.text()
                pok = '"result":true' in body.replace(" ", "")
            print(
                ("NEW" if created_new else "UPD"),
                rname,
                f"n={len(items)}",
                "photo-ok" if pok else f"photo-fail {body[:160]}",
            )

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()


asyncio.run(main())
