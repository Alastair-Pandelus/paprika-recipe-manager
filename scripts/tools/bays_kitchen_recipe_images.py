"""Fetch each adapted Bay's Kitchen recipe's own photo (the hero image above "Prep time"),
crop it square and save it to data/bays_kitchen/recipe_images/<handle>.jpg.

  python scripts/tools/bays_kitchen_recipe_images.py
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
DRAFTS = ROOT / "data" / "bays_kitchen" / "recipe_drafts.json"
IMAGES = ROOT / "data" / "bays_kitchen" / "recipe_images"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def hero_url(dom: str) -> str | None:
    i = dom.find("Prep time")
    seg = dom[max(0, i - 25000): i] if i > 0 else dom
    urls = re.findall(r"//www\.bayskitchen\.com/cdn/shop/(?:products|files|articles)/[^\s\"?]+\.(?:jpe?g|png|webp)", seg)
    urls = [u for u in urls if not re.search(r"/Image_[0-9a-f-]+\.png", u)]
    return ("https:" + urls[-1]) if urls else None


def main() -> None:
    drafts = json.loads(DRAFTS.read_text(encoding="utf-8"))
    IMAGES.mkdir(parents=True, exist_ok=True)
    found = {}
    for x in drafts:
        h = x["handle"]
        dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=15000", "--dump-dom", x["url"]],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout
        url = hero_url(dom)
        time.sleep(1.5)
        if not url:
            print("NO HERO", h); continue
        data = urllib.request.urlopen(urllib.request.Request(url + "?width=1600", headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, hgt = im.size; s = min(w, hgt)
        im = im.crop(((w - s) // 2, (hgt - s) // 2, (w - s) // 2 + s, (hgt - s) // 2 + s)).resize((1200, 1200), Image.LANCZOS)
        im.save(IMAGES / f"{h}.jpg", quality=88)
        found[h] = url
        print("ok", h, url.rsplit("/", 1)[-1])
    (ROOT / "data" / "bays_kitchen" / "recipe_image_urls.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    print("images", len(found), "of", len(drafts))


if __name__ == "__main__":
    main()
