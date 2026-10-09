"""Import Field Doctor Health Hub IBS/Low FODMAP recipes into Paprika.

  python scripts/tools/import_fd_health_hub_lf.py
  python scripts/tools/import_fd_health_hub_lf.py --apply
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
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

HUB_URL = "https://www.fielddoctor.co.uk/health-hub?categories=ibsLowFodmap"
OUT_DIR = Path(__file__).resolve().parent / ".fd_health_hub_lf_dryrun"
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def iso_duration_to_minutes(val: str | None) -> str:
    if not val:
        return ""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", val or "", re.I)
    if not m:
        return ""
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    total = hours * 60 + mins
    return str(total) if total else ""


def clean_line(s: str) -> str:
    s = html_lib.unescape(s or "")
    s = s.replace("\xa0", " ")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" \t\r\n•-")
    return s


def parse_ld_recipe(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    nodes = data if isinstance(data, list) else [data]
    flat: list = []
    for n in nodes:
        if isinstance(n, dict) and "@graph" in n:
            flat.extend(n["@graph"])
        else:
            flat.append(n)
    for n in flat:
        if isinstance(n, dict) and n.get("@type") == "Recipe":
            return n
    return None


def strip_scripts_styles(html: str) -> str:
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    return html


NAV_NOISE = {
    "low fodmap",
    "eat well, live well programme",
    "gluten free",
    "high protein",
    "weight management",
    "lowest calorie",
    "lower carbs",
    "mediterranean",
    "smaller range",
    "pmos (formerly pcos)",
    "menopause",
    "heart healthy",
    "full menu",
    "gift cards",
    "health hub",
    "about about us",
    "core principles",
    "our experts",
    "customer stories",
    "guides + resources",
    "help contact us",
    "our meals low fodmap",
    "ibs",
    "glp-1",
}


def looks_like_nav(text: str) -> bool:
    low = text.lower().strip()
    if low in NAV_NOISE:
        return True
    if len(low) < 40 and low in {
        "about us",
        "contact us",
        "faqs",
        "shop",
        "login",
        "get started",
    }:
        return True
    return False


def parse_ld_steps(ld: dict | None) -> list[str]:
    if not ld:
        return []
    steps: list[str] = []
    instr = ld.get("recipeInstructions") or []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if x.get("@type") in {"HowToStep", "HowToDirection"} or "text" in x:
                t = clean_line(x.get("text") or "")
                if t and not looks_like_nav(t):
                    steps.append(t)
            for k, v in x.items():
                if k in {"text", "name", "@type", "url", "@id"}:
                    continue
                if isinstance(v, (list, dict)):
                    walk(v)

    walk(instr)
    # Dedupe while preserving order
    out: list[str] = []
    seen: set[str] = set()
    for s in steps:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse_html_ingredients_method(html: str) -> tuple[list[str], list[str]]:
    visible = strip_scripts_styles(html)
    m = re.search(r">\s*Ingredients\s*<(.*?)>\s*Method\s*<", visible, re.S | re.I)
    if not m:
        m = re.search(r"Ingredients(.*?)Method", visible, re.S | re.I)
    ings: list[str] = []
    if m:
        chunk = re.sub(r"<[^>]+>", "\n", m.group(1))
        for line in chunk.splitlines():
            t = clean_line(line)
            if not t or t.lower() in {"ingredients", "optional to serve:"}:
                continue
            if looks_like_nav(t):
                continue
            if re.search(r"[<>]|/h\d|class=", t, re.I):
                continue
            ings.append(t)

    steps: list[str] = []
    m2 = re.search(
        r">\s*Method\s*<(.*?)(?:>\s*(?:An Easy|related recipes|more recipes)\s*<|nutritional science)",
        visible,
        re.S | re.I,
    )
    if not m2:
        m2 = re.search(
            r"Method(.*?)(?:An Easy|related recipes|more recipes|nutritional science)",
            visible,
            re.S | re.I,
        )
    if m2:
        lis = re.findall(r"<li[^>]*>(.*?)</li>", m2.group(1), re.S | re.I)
        for li in lis:
            t = clean_line(li)
            if not t or looks_like_nav(t):
                continue
            # Cooking steps are usually full sentences
            if len(t) < 20 and not re.search(r"[.!?]$", t):
                continue
            steps.append(t)
        if not steps:
            chunk = re.sub(r"<[^>]+>", "\n", m2.group(1))
            for line in chunk.splitlines():
                t = clean_line(line)
                if (
                    t
                    and len(t) > 20
                    and t.lower() != "method"
                    and not looks_like_nav(t)
                ):
                    steps.append(t)
    return ings, steps


def recipe_image(ld: dict | None, html: str) -> str | None:
    if ld:
        imgs = ld.get("image") or []
        if isinstance(imgs, str):
            return imgs
        if isinstance(imgs, dict):
            return imgs.get("url") or None
        if isinstance(imgs, list) and imgs:
            first = imgs[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("url")
    m = re.search(
        r'property="og:image"\s+content="([^"]+)"',
        html,
        re.I,
    )
    if m and "meta_preview" not in m.group(1):
        return html_lib.unescape(m.group(1))
    return None


def paprika_name(site_name: str) -> str:
    name = clean_line(site_name)
    if not name.lower().startswith("field doctor"):
        name = f"Field Doctor {name}"
    return name


def expand_numbered_blob(text: str) -> list[str]:
    """Split a single blob like '1. Foo 2. Bar' into separate steps."""
    text = clean_line(text)
    # Drop a leading '1.' if present before splitting
    parts = re.split(r"(?:(?<=\s)|^)\d{1,2}\.\s+", text)
    parts = [clean_line(p) for p in parts if clean_line(p)]
    if len(parts) >= 2:
        return parts
    return [text] if text else []


def directions_text(steps: list[str]) -> str:
    expanded: list[str] = []
    for step in steps:
        if re.search(r"(?:^|\s)2\.\s+", step) and re.search(r"(?:^|\s)?1\.\s+", step):
            expanded.extend(expand_numbered_blob(step))
        else:
            expanded.append(clean_line(step))
    lines = []
    for i, step in enumerate(expanded, 1):
        # Strip accidental leading numbering
        step = re.sub(r"^\d{1,2}\.\s*", "", step).strip()
        if step:
            lines.append(f"**{i}.** {step}")
    return "\n\n".join(lines)


def scrape_recipe(html: str, url: str) -> dict | None:
    ld = parse_ld_recipe(html)
    html_ings, html_steps = parse_html_ingredients_method(html)
    ld_ings = [
        clean_line(x)
        for x in ((ld or {}).get("recipeIngredient") or [])
        if clean_line(x)
    ]
    ld_steps = parse_ld_steps(ld)

    # Ingredients: prefer the richer list (HTML often includes items LD omits).
    if html_ings and ld_ings:
        ings = html_ings if len(html_ings) >= len(ld_ings) else ld_ings
    else:
        ings = html_ings or ld_ings

    # Steps: prefer LD HowToStep when present; else filtered HTML.
    if ld_steps:
        steps = ld_steps
    else:
        steps = html_steps

    if not ings or not steps:
        return None

    site_name = (ld or {}).get("name") or ""
    if not site_name:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        site_name = clean_line(m.group(1)) if m else url.rstrip("/").split("/")[-1]

    author = ""
    if ld and isinstance(ld.get("author"), dict):
        author = ld["author"].get("name") or ""
    elif ld and isinstance(ld.get("author"), str):
        author = ld["author"]

    desc = clean_line((ld or {}).get("description") or "")
    # Trim glued headings sometimes present in LD description
    desc = re.sub(r"^(Low FODMAP[^.]*Recipe)", "", desc).strip()

    yield_raw = (ld or {}).get("recipeYield")
    if isinstance(yield_raw, list):
        yield_raw = yield_raw[0] if yield_raw else ""
    servings = str(yield_raw or "").strip() or "1"

    notes_parts = [
        "Imported from Field Doctor Health Hub (IBS + Low FODMAP) for personal use.",
    ]
    if author:
        notes_parts.append(f"Author: {author}.")
    notes_parts.append(f"Source: {url}")

    return {
        "name": paprika_name(site_name),
        "site_name": site_name,
        "ingredients": "\n".join(ings),
        "directions": directions_text(steps),
        "description": desc,
        "notes": " ".join(notes_parts),
        "servings": servings,
        "prep_time": iso_duration_to_minutes((ld or {}).get("prepTime")),
        "cook_time": iso_duration_to_minutes((ld or {}).get("cookTime")),
        "source_url": url,
        "source": "Field Doctor Health Hub",
        "image_url": recipe_image(ld, html),
        "author": author,
        "ingredient_count": len(ings),
        "step_count": len(steps),
    }


async def list_recipe_urls(session: aiohttp.ClientSession) -> list[str]:
    async with session.get(HUB_URL, headers={"User-Agent": "Mozilla/5.0"}) as r:
        html = await r.text()
        if r.status != 200:
            raise SystemExit(f"hub fetch failed {r.status}")
    paths = sorted(set(re.findall(r'href="(/health-hub/recipe/[^"]+)"', html)))
    return [f"https://www.fielddoctor.co.uk{p}" for p in paths]


async def fetch_photo_jpeg(
    session: aiohttp.ClientSession, url: str | None
) -> tuple[bytes, str, str] | None:
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
    want = {"Field Doctor", "Low Fodmap"}
    out: dict[str, str] = {}
    for c in body.get("result") or []:
        if c.get("deleted"):
            continue
        name = c.get("name") or ""
        if name in want:
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
    safe_print(f"Import FD Health Hub LF recipes | {mode}")

    limiter = RateLimiter(0.28)
    user, pw = paprika_credentials()

    # Existing source URLs (local + we'll also check cloud on apply)
    con = sqlite3.connect(LOCAL_DB)
    existing_urls = {
        (r[0] or "").strip()
        for r in con.execute(
            "SELECT source_url FROM recipes WHERE coalesce(in_trash,0)=0 AND source_url IS NOT NULL"
        )
    }
    template_uid_row = con.execute(
        """
        SELECT r.uid FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        JOIN recipe_categories c ON c.uid = rtc.category_uid
        WHERE c.name = 'Field Doctor' AND coalesce(r.in_trash,0)=0
        LIMIT 1
        """
    ).fetchone()
    con.close()
    if not template_uid_row:
        raise SystemExit("no Field Doctor template in local DB")

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
        cat_list = [cats["Field Doctor"], cats["Low Fodmap"]]

        st, body = await api_json(
            session,
            limiter,
            "GET",
            f"{PAPRIKA_API}/v2/sync/recipe/{template_uid_row[0]}/",
            headers=headers,
        )
        template = (body or {}).get("result")
        if not template:
            raise SystemExit("failed to load template recipe")

        urls = await list_recipe_urls(session)
        safe_print(f"found {len(urls)} health-hub recipe URLs")

        created = updated = skipped = failed = 0
        artifacts = []

        for url in urls:
            await limiter.wait_turn()
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
                status = r.status
            if status != 200:
                safe_print("FAIL fetch", status, url)
                failed += 1
                continue

            scraped = scrape_recipe(html, url)
            if not scraped:
                safe_print("FAIL parse", url)
                failed += 1
                continue

            handle = url.rstrip("/").split("/")[-1]
            (OUT_DIR / f"{handle}.json").write_text(
                json.dumps(scraped, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            artifacts.append(scraped)
            safe_print(
                "OK",
                scraped["name"][:55],
                f"ings={scraped['ingredient_count']}",
                f"steps={scraped['step_count']}",
                f"serv={scraped['servings']}",
            )

            if not apply:
                continue

            is_new = url not in existing_urls
            if is_new:
                recipe = {k: template[k] for k in template}
                for k in ("photo", "photo_hash", "photo_large", "image_url"):
                    recipe[k] = None
                recipe["photo_url"] = ""
                recipe["uid"] = str(uuid.uuid4()).upper()
            else:
                # Find existing by scanning local uid map
                con = sqlite3.connect(LOCAL_DB)
                row = con.execute(
                    "SELECT uid FROM recipes WHERE source_url=? AND coalesce(in_trash,0)=0",
                    (url,),
                ).fetchone()
                con.close()
                if not row:
                    # treat as new if local missing but URL marked somehow
                    recipe = {k: template[k] for k in template}
                    for k in ("photo", "photo_hash", "photo_large", "image_url"):
                        recipe[k] = None
                    recipe["photo_url"] = ""
                    recipe["uid"] = str(uuid.uuid4()).upper()
                    is_new = True
                else:
                    st, body = await api_json(
                        session,
                        limiter,
                        "GET",
                        f"{PAPRIKA_API}/v2/sync/recipe/{row[0]}/",
                        headers=headers,
                    )
                    recipe = (body or {}).get("result")
                    if not recipe:
                        safe_print("FAIL load", url)
                        failed += 1
                        continue

            if not is_new and url in existing_urls:
                # Still update content for freshness
                pass

            recipe["name"] = scraped["name"]
            recipe["ingredients"] = scraped["ingredients"]
            recipe["directions"] = scraped["directions"]
            recipe["description"] = scraped["description"]
            recipe["notes"] = scraped["notes"]
            recipe["servings"] = scraped["servings"]
            recipe["prep_time"] = scraped["prep_time"]
            recipe["cook_time"] = scraped["cook_time"]
            recipe["prep_minutes"] = int(scraped["prep_time"] or 0) or None
            recipe["cook_minutes"] = int(scraped["cook_time"] or 0) or None
            recipe["categories"] = cat_list
            recipe["source"] = scraped["source"]
            recipe["source_url"] = scraped["source_url"]
            recipe["in_trash"] = False
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            photo_pack = await fetch_photo_jpeg(session, scraped.get("image_url"))
            if photo_pack:
                jpeg, photo_name, photo_hash = photo_pack
                recipe["photo"] = photo_name
                recipe["photo_large"] = photo_name
                recipe["photo_hash"] = photo_hash
                recipe["photo_url"] = None
                recipe["image_url"] = scraped.get("image_url")
            else:
                recipe["photo"] = None
                recipe["photo_hash"] = None
                recipe["photo_large"] = None
                recipe["photo_url"] = None
                recipe["image_url"] = scraped.get("image_url")

            recipe["hash"] = calc_hash(recipe)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(recipe),
                content_type="application/octet-stream",
                filename="data",
            )
            if photo_pack:
                form.add_field(
                    "photo_upload",
                    photo_pack[0],
                    filename=photo_pack[1],
                    content_type="image/jpeg",
                )
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
                headers=headers,
                data=form,
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            if not ok:
                safe_print("FAIL save", scraped["name"])
                failed += 1
                continue

            existing_urls.add(url)
            if is_new:
                created += 1
                safe_print("NEW", scraped["name"][:50], recipe["uid"], "photo" if photo_pack else "nophoto")
            else:
                updated += 1
                safe_print("UPD", scraped["name"][:50], recipe["uid"], "photo" if photo_pack else "nophoto")

        summary = {
            "mode": mode,
            "urls": len(urls),
            "parsed": len(artifacts),
            "created": created,
            "updated": updated,
            "failed": failed,
            "names": [a["name"] for a in artifacts],
        }
        (OUT_DIR / "_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if apply:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                safe_print("notify", r.status)
            safe_print(
                f"Done | created={created} updated={updated} failed={failed} parsed={len(artifacts)}"
            )
        else:
            safe_print(f"Dry-run artifacts in {OUT_DIR} | parsed={len(artifacts)}")


if __name__ == "__main__":
    asyncio.run(main())
