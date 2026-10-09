"""Import new Field Doctor Low FODMAP meals into Paprika (1-serve reverse-engineer).

  python scripts/tools/import_fd_new_lf_meals.py
  python scripts/tools/import_fd_new_lf_meals.py --apply
"""
from __future__ import annotations

import argparse
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

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "field_doctor"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import rescale_density as fd  # noqa: E402
from fd_single_serve_scrape import rebuild_from_html  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

PORTIONS = 1
OUT_DIR = Path(__file__).resolve().parent / ".fd_new_lf_meals_dryrun"

# Label-list typos that glue unrelated items together when parsing.
# Note: rescale_density.clean_text rewrites "Salt)" → "Salt]", which leaves an
# unclosed "(" and stops top-level comma splits — so use square brackets here.
RAW_FIXES: dict[str, list[tuple[str, str]]] = {
    "chicken-tomato-borlotti-bean-pasta-bowl-low-fodmap": [
        (
            "Chicken Stock (Chicken Extract, Chicken Fat, Salt], Sweet Noble Paprika, "
            "Herb Mix (0.5%), Red Wine Vinegar, Basil (0.5%)",
            "Chicken Stock [Chicken Extract, Chicken Fat, Salt], Sweet Noble Paprika, "
            "Herb Mix (0.5%), Red Wine Vinegar, Basil (0.5%)",
        ),
        (
            "Chicken Stock (Chicken Extract, Chicken Fat, Salt), Sweet Noble Paprika, "
            "Herb Mix (0.5%), Red Wine Vinegar, Basil (0.5%)",
            "Chicken Stock [Chicken Extract, Chicken Fat, Salt], Sweet Noble Paprika, "
            "Herb Mix (0.5%), Red Wine Vinegar, Basil (0.5%)",
        ),
    ],
}

MEALS = [
    {
        "handle": "sweet-sour-chicken-low-fodmap",
        "title": "Sweet + Sour Chicken",
        "photo_substr": "FOD_Sweet_and_Sour_Chicken",
        "directions": """**Prep (bowls):**

**Large bowl — grains (boil later):**
- Rinse the wholegrain rice and red quinoa.

**Large bowl — chicken (fry / brown):**
- Dice the chicken into even pieces; pat dry.

**Large bowl — veg (stir-fry):**
- Cut the broccoli into small florets.
- Dice the red pepper and courgette.
- Have the sweetcorn and edamame ready.

**Small bowl — aromatics (fry):**
- Peel and finely grate or mince the ginger.
- Slice the chives.

**Small bowl — sauce:**
- Mix the tamari, red wine vinegar, brown sugar, tomato puree and tapioca starch with a splash of cold water.

By the hob (no bowl): extra virgin olive oil, sea salt.

**1. Grains:** Cook the grains bowl in salted water until tender; drain and keep warm.

**2. Brown:** Warm extra virgin olive oil. Brown the chicken bowl until cooked through; set aside.

**3. Veg:** In the same pan, stir-fry the veg bowl with the aromatics bowl until tender-crisp.

**4. Sauce:** Return the chicken bowl. Stir in the sauce bowl and cook until glossy and lightly thickened.

**5. Serve:** Spoon over the grains. Season with salt.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
        "ingredient_order": [
            "whole grain rice",
            "red quinoa",
            "chicken",
            "broccoli",
            "red pepper",
            "courgette",
            "sweetcorn",
            "edamame",
            "ginger",
            "chive",
            "tamari",
            "red wine vinegar",
            "brown sugar",
            "tomato puree",
            "tapioca",
            "olive oil",
            "salt",
        ],
    },
    {
        "handle": "chicken-tomato-borlotti-bean-pasta-bowl-low-fodmap",
        "title": "Chicken, Tomato + Borlotti Bean Pasta Bowl",
        "photo_substr": "FODChickenTomatoBorlottiBeanPastaBowl",
        "directions": """**Prep (bowls):**

**Large bowl — pasta (boil later):**
- Have the gluten-free penne ready to cook.

**Large bowl — chicken (fry / brown):**
- Dice the chicken into even pieces; pat dry.

**Large bowl — veg (soften / simmer):**
- Dice the carrot, green pepper and red pepper.
- Slice or shred the fennel.
- Have the spinach and borlotti beans ready.

**Small bowl — spice mix (fry):**
- Mix the sweet paprika and herb mix.
- Have the tomato puree ready beside this bowl.

**Medium bowl — finish:**
- Have the basil and Parmigiano ready.
- Optional: mix the tapioca starch with a splash of cold water (small dish).

By the hob (no bowl): chopped tomatoes, chicken stock, red wine vinegar, extra virgin olive oil, sea salt, black pepper.

**1. Pasta:** Cook the pasta bowl in salted water until al dente; drain and keep warm.

**2. Brown:** Warm extra virgin olive oil. Brown the chicken bowl until cooked through; set aside.

**3. Soften:** In the same pan, soften the veg bowl (keep spinach for later).

**4. Fry spices:** Stir in the tomato puree, then the spice mix bowl. Cook briefly until fragrant.

**5. Sauce:** Add the chopped tomatoes, chicken stock and red wine vinegar. Return the chicken bowl with the borlotti beans. Simmer until thickened; use the slurry only if needed.

**6. Finish:** Fold through the pasta bowl and wilt the spinach from the veg bowl. Tear in basil from the finish bowl. Season with salt and pepper. Serve topped with Parmigiano from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
        "ingredient_order": [
            "penne",
            "chicken",
            "carrot",
            "green pepper",
            "red pepper",
            "fennel",
            "spinach",
            "borlotti",
            "paprika",
            "herb",
            "tomato puree",
            "basil",
            "parmigiano",
            "italian hard cheese",
            "tapioca",
            "chopped tomato",
            "plum tomato",
            "chicken stock",
            "red wine vinegar",
            "olive oil",
            "sea salt",
            "black pepper",
        ],
    },
    {
        "handle": "malaysian-tofu-curry-low-fodmap",
        "title": "Malaysian Tofu Curry",
        "photo_substr": "FOD_Tofu_Curry",
        "directions": """**Prep (bowls):**

**Large bowl — grains (boil later):**
- Rinse the wholegrain rice and red quinoa.

**Large bowl — tofu (fry / brown):**
- Drain and cube the tofu; pat dry. Optionally toss with a little tamari.

**Large bowl — veg (simmer in curry):**
- Peel and dice the sweet potato.
- Dice the green and red peppers.
- Trim the green beans.
- Have the edamame ready.

**Small bowl — aromatics (fry):**
- Peel and finely grate or mince the ginger and galangal.
- Prep the lemongrass, lime leaf and chilli (to taste).
- Slice the chives.
- Mix the turmeric blend and ground fennel (small dish).

**Medium bowl — finish:**
- Have the coriander and basil ready.
- Optional: mix the tapioca starch with a splash of cold water (small dish).

By the hob (no bowl): coconut milk, tamari, brown sugar, lime juice, tamarind extract, extra virgin olive oil, sea salt.

**1. Grains:** Cook the grains bowl in salted water until tender; drain and keep warm.

**2. Brown tofu:** Warm extra virgin olive oil. Brown the tofu bowl; set aside.

**3. Aromatics:** Soften the aromatics bowl in the same pan. Add the dry spice mix and cook briefly until fragrant.

**4. Curry:** Add the veg bowl. Pour in the coconut milk, remaining tamari, brown sugar, lime juice and tamarind (and water as needed). Simmer until the sweet potato is tender.

**5. Finish:** Return the tofu bowl. Thicken with the slurry only if needed. Stir through coriander and basil from the finish bowl. Serve over the grains. Season with salt.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
        "ingredient_order": [
            "whole grain rice",
            "red quinoa",
            "tofu",
            "sweet potato",
            "green pepper",
            "red pepper",
            "green bean",
            "edamame",
            "ginger",
            "galangal",
            "lemongrass",
            "lime leaf",
            "chilli",
            "chive",
            "turmeric",
            "fennel",
            "coriander",
            "basil",
            "tapioca",
            "coconut milk",
            "tamari",
            "brown sugar",
            "lime juice",
            "tamarind",
            "olive oil",
            "salt",
        ],
    },
]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def fix_raw(handle: str, raw: str) -> str:
    out = raw
    for old, new in RAW_FIXES.get(handle, []):
        out = out.replace(old, new)
    return out


def rebuild_fixed(html: str, *, handle: str, recipe_name: str, portions: int) -> dict | None:
    raw, serving = fd.extract_from_html(html)
    if not raw:
        return None
    raw = fix_raw(handle, raw)

    items = fd.merge_duplicate_ingredients(
        fd.allocate_percentages(fd.parse_ingredients(raw))
    )
    omitted_water = next((it for it in items if fd.is_plain_water(it.name)), None)
    items = [it for it in items if not fd.is_plain_water(it.name)]

    omitted_oil = None
    kept_oils: list = []
    rebuilt: list = []
    for it in items:
        if not fd.is_olive_oil(it.name):
            rebuilt.append(it)
            continue
        if fd.should_keep_olive_oil(
            it, serving_g=serving, portions=portions, recipe_name=recipe_name
        ):
            rebuilt.append(it)
            kept_oils.append(it)
        else:
            if omitted_oil is None or it.assigned > omitted_oil.assigned:
                omitted_oil = it
    items = rebuilt

    text = fd.build_ingredient_text(items, serving, portions)
    scaling = fd.build_scaling_notes(
        items,
        serving,
        portions,
        omitted_water=omitted_water,
        omitted_oil=omitted_oil,
    )
    return {
        "source_raw": raw,
        "serving_g": serving,
        "portions": portions,
        "ingredients_text": text,
        "scaling_notes": scaling,
        "oil_status": "kept" if kept_oils else ("omitted" if omitted_oil else "none"),
        "items": items,
    }


def reorder_ingredients(text: str, order_keys: list[str]) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]

    def rank(line: str) -> tuple[int, int]:
        low = line.lower()
        for i, key in enumerate(order_keys):
            if key in low:
                return (i, 0)
        return (len(order_keys), 0)

    return "\n".join(sorted(lines, key=rank))


def notes_for(title: str, scaling: str, raw: str) -> str:
    preamble = (
        "REVERSE-ENGINEERED from Field Doctor published ingredients for personal home cooking only. "
        "Official meal is Monash Low FODMAP certified at single-serve size."
    )
    return fd.merge_recipe_notes(preamble, scaling, raw)


def extract_photo_url(html: str, substr: str) -> str | None:
    imgs = re.findall(
        r"https://cdn\.shopify\.com/s/files/[^\"'\\ ]+\.(?:jpg|jpeg|png|webp)(?:\?[^\"'\\ ]*)?",
        html,
        flags=re.I,
    )
    clean: list[str] = []
    for u in imgs:
        u = html_lib.unescape(u).rstrip("\\")
        if "meta_preview" in u:
            continue
        if "width=" in u or "height=" in u:
            continue
        clean.append(u)
    for u in clean:
        if substr.lower() in u.lower():
            return u
    return clean[0] if clean else None


async def fetch_photo_jpeg(session: aiohttp.ClientSession, url: str) -> tuple[bytes, str, str] | None:
    """Return (jpeg_bytes, photo_filename, sha256_hex) or None."""
    if not url:
        return None
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                return None
            data = await r.read()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((900, 900))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        jpeg = buf.getvalue()
        photo_name = f"{str(uuid.uuid4()).upper()}.jpg"
        photo_hash = hashlib.sha256(jpeg).hexdigest().upper()
        return jpeg, photo_name, photo_hash
    except Exception as e:
        safe_print("photo fail", e)
        return None


async def category_uids(session, headers, limiter) -> dict[str, str]:
    st, body = await api_json(
        session, limiter, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=headers
    )
    if st != 200:
        raise SystemExit(f"categories failed {st}")
    want = {"Field Doctor", "Low Fodmap", "Main Meals"}
    out: dict[str, str] = {}
    for c in body.get("result") or []:
        if c.get("deleted"):
            continue
        name = c.get("name") or ""
        if name in want and name not in out:
            out[name] = c["uid"]
    missing = want - set(out)
    if missing:
        raise SystemExit(f"missing categories: {missing}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"Import FD new LF meals | {mode}")

    limiter = RateLimiter(0.28)
    user, pw = paprika_credentials()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        st, body = await api_json(
            session,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        cats = await category_uids(session, headers, limiter)
        cat_list = [cats["Field Doctor"], cats["Low Fodmap"], cats["Main Meals"]]

        # Local SQLite: find existing recipes by source_url (fast)
        import sqlite3

        local_db = Path(
            r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
        )
        con = sqlite3.connect(local_db)
        con.row_factory = sqlite3.Row
        by_url_uid = {
            (r["source_url"] or "").strip(): r["uid"]
            for r in con.execute(
                "SELECT uid, source_url FROM recipes WHERE coalesce(in_trash,0)=0 AND source_url IS NOT NULL"
            )
            if (r["source_url"] or "").strip()
        }
        template_uid = con.execute(
            """
            SELECT r.uid FROM recipes r
            JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
            WHERE rtc.category_uid = ? AND coalesce(r.in_trash,0)=0
            LIMIT 1
            """,
            (cats["Field Doctor"],),
        ).fetchone()
        con.close()
        if not template_uid:
            raise SystemExit("no Field Doctor template recipe found")
        st, body = await api_json(
            session,
            limiter,
            "GET",
            f"{PAPRIKA_API}/v2/sync/recipe/{template_uid['uid']}/",
            headers=headers,
        )
        template = (body or {}).get("result")
        if not template:
            raise SystemExit("failed to load Field Doctor template")

        created = updated = 0
        for meal in MEALS:
            handle = meal["handle"]
            title = meal["title"]
            source_url = f"https://www.fielddoctor.co.uk/products/{handle}"
            await limiter.wait_turn()
            async with session.get(
                source_url, headers={"User-Agent": "Mozilla/5.0"}
            ) as r:
                html = await r.text()
                status = r.status
            if status != 200:
                safe_print("FAIL fetch", handle, status)
                continue

            rebuilt = rebuild_fixed(
                html, handle=handle, recipe_name=title, portions=PORTIONS
            )
            if not rebuilt:
                safe_print("FAIL parse", handle)
                continue

            ings = reorder_ingredients(
                rebuilt["ingredients_text"], meal["ingredient_order"]
            )
            dirs = meal["directions"].strip()
            if rebuilt["oil_status"] == "omitted":
                dirs = fd.ensure_fry_in_evoo(dirs)
            notes = notes_for(title, rebuilt["scaling_notes"], rebuilt["source_raw"])
            rname = f"Field Doctor {title} (Low FODMAP)"
            desc = (
                f"Home recreation inspired by Field Doctor {title} (Low FODMAP) "
                f"(~{rebuilt['serving_g']} g ready meal). Scaled to {PORTIONS} portions. "
                f"Not an official Field Doctor recipe. {source_url}"
            )

            artifact = {
                "handle": handle,
                "name": rname,
                "source_url": source_url,
                "serving_g": rebuilt["serving_g"],
                "oil_status": rebuilt["oil_status"],
                "ingredients": ings,
                "directions": dirs,
                "notes": notes,
                "description": desc,
                "source_raw": rebuilt["source_raw"],
            }
            (OUT_DIR / f"{handle}.json").write_text(
                json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            safe_print(
                "OK",
                rname,
                f"~{rebuilt['serving_g']}g",
                "oil",
                rebuilt["oil_status"],
                f"n={len(rebuilt['items'])}",
            )

            if not apply:
                continue

            existing_uid = by_url_uid.get(source_url)
            is_new = existing_uid is None
            if is_new:
                recipe = {k: template[k] for k in template}
                for k in ("photo", "photo_hash", "photo_large", "image_url"):
                    recipe[k] = None
                recipe["photo_url"] = ""
                recipe["uid"] = str(uuid.uuid4()).upper()
            else:
                st, body = await api_json(
                    session,
                    limiter,
                    "GET",
                    f"{PAPRIKA_API}/v2/sync/recipe/{existing_uid}/",
                    headers=headers,
                )
                recipe = (body or {}).get("result")
                if not recipe:
                    safe_print("FAIL load existing", handle)
                    continue

            recipe["name"] = rname
            recipe["ingredients"] = ings
            recipe["directions"] = dirs
            recipe["notes"] = notes
            recipe["description"] = desc
            recipe["servings"] = str(PORTIONS)
            recipe["categories"] = cat_list
            recipe["source"] = "Field Doctor (inspired recreation)"
            recipe["source_url"] = source_url
            recipe["in_trash"] = False
            recipe["prep_time"] = "25"
            recipe["cook_time"] = "35"
            recipe["prep_minutes"] = 25
            recipe["cook_minutes"] = 35
            recipe["photo"] = None
            recipe["photo_hash"] = None
            recipe["photo_large"] = None
            recipe["photo_url"] = ""
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
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
                headers=headers,
                data=form,
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            if not ok:
                safe_print("FAIL save", handle)
                continue

            photo_url = extract_photo_url(html, meal["photo_substr"])
            photo_pack = await fetch_photo_jpeg(session, photo_url or "")
            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
                headers=headers,
            )
            recipe = (body or {}).get("result") or recipe
            pok = False
            if photo_pack:
                photo_bytes, photo_name, photo_hash = photo_pack
                recipe["photo"] = photo_name
                recipe["photo_large"] = photo_name
                recipe["photo_hash"] = photo_hash
                recipe["photo_url"] = None
                recipe["image_url"] = None
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
                    filename=photo_name,
                    content_type="image/jpeg",
                )
                await limiter.wait_turn()
                async with session.post(
                    f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
                    headers=headers,
                    data=form2,
                ) as r:
                    pok = '"result":true' in (await r.text()).replace(" ", "")

            if is_new:
                created += 1
                safe_print("NEW", rname, recipe["uid"], "photo-ok" if pok else "photo-fail")
            else:
                updated += 1
                safe_print("UPD", rname, recipe["uid"], "photo-ok" if pok else "photo-fail")

        if apply:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                safe_print("notify", r.status)
            safe_print(f"Done | created={created} updated={updated}")
        else:
            safe_print(f"Dry-run artifacts in {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
