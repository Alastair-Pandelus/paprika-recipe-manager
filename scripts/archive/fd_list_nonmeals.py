"""List FD Low FODMAP products vs existing Paprika FD recipes."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
COLL = "https://www.fielddoctor.co.uk/collections/low-fodmap/products.json"


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def classify(title: str, handle: str, tags: list) -> str:
    t = f"{title} {handle} {' '.join(tags)}".lower()
    if any(x in t for x in ("personalised", "personalized", "gift", "programme", "plan", "quiz", "subscription", "bundle", "box")):
        return "skip-meta"
    if "porridge" in t or "overnight oat" in t:
        return "porridge"
    if " bar" in f" {t}" or t.endswith("bar") or "protein bar" in t or "flapjack" in t:
        return "bar"
    if "smoothie" in t or "shake" in t:
        return "drink"
    if "soup" in t:
        return "soup"
    # meals usually have these
    return "meal-or-other"


async def main() -> None:
    load_env()
    products = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        page = 1
        while True:
            url = f"{COLL}?limit=250&page={page}"
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                batch = (await r.json()).get("products") or []
            if not batch:
                break
            products.extend(batch)
            page += 1

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
        existing = []
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD in (recipe.get("categories") or []):
                existing.append(
                    {
                        "name": recipe.get("name"),
                        "source_url": recipe.get("source_url"),
                    }
                )

    print(f"FD collection products: {len(products)}")
    print(f"Paprika Field Doctor recipes: {len(existing)}")
    print()
    by_class: dict[str, list] = {}
    for p in products:
        c = classify(p.get("title") or "", p.get("handle") or "", p.get("tags") or [])
        by_class.setdefault(c, []).append(p)

    for c, items in sorted(by_class.items()):
        print(f"=== {c} ({len(items)}) ===")
        for p in items:
            print(f"  {p.get('handle')} | {p.get('title')}")
        print()

    existing_urls = {e.get("source_url") or "" for e in existing}
    existing_names = {(e.get("name") or "").lower() for e in existing}
    print("=== NOT YET IN PAPRIKA (by URL/name) ===")
    for p in products:
        url = f"https://www.fielddoctor.co.uk/products/{p.get('handle')}"
        title = p.get("title") or ""
        c = classify(title, p.get("handle") or "", p.get("tags") or [])
        if c == "skip-meta":
            continue
        # rough name match
        core = title.lower().replace("(low fodmap)", "").strip()
        hit = url in existing_urls or any(core in n for n in existing_names)
        if not hit:
            print(f"  [{c}] {p.get('handle')} | {title}")


asyncio.run(main())
