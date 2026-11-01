"""Read Bay's Kitchen recipe pages (rendered with headless Chrome) into data/bays_kitchen/recipes.json.

The recipe text is filled in by script, so a plain fetch doesn't see it. Each page is
rendered with Chrome's --dump-dom about one per two seconds. Personal use only.

  python scripts/tools/scrape_bays_kitchen_recipes.py
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "bays_kitchen" / "recipes.json"
LINKS = ROOT / "data" / "bays_kitchen" / "recipe_links.txt"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SAUCES = {
    r"tomato\s*(?:&|and)\s*basil": "tomato-basil-stir-in-sauce", r"korma": "mild-korma-stir-in-sauce",
    r"tikka\s*masala": "tikka-masala-stir-in-sauce", r"katsu": "katsu-curry-stir-in-sauce",
    r"sweet\s*(?:&|and)\s*sour": "sweet-sour-stir-in-sauce", r"massaman": "thai-massaman-stir-in-sauce",
    r"thai\s*green": "thai-green-curry-stir-in-sauce", r"arrabbiata": "spicy-arrabbiata-stir-in-sauce",
    r"thai\s*red": "thai-red-curry-stir-in-sauce", r"teriyaki": "teriyaki-stir-in-sauce",
}


def text_of(dom: str) -> str:
    dom = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", dom, flags=re.S | re.I)
    dom = re.sub(r"<(br|/p|/li|/h\d|/div|/span)[^>]*>", "\n", dom, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", dom))
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def section(t: str, start: str, ends: list[str]) -> str:
    i = t.find(start)
    if i < 0:
        return ""
    j = min([k for k in (t.find(e, i + len(start)) for e in ends) if k > 0] or [len(t)])
    return t[i + len(start): j].strip()


def main() -> None:
    handles = [l.rsplit("/", 1)[-1] for l in LINKS.read_text().split("\n") if l and "/tagged/" not in l]
    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for h in handles:
        if h in out:
            continue
        url = f"https://www.bayskitchen.com/pages/recipes/{h}"
        dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=15000", "--dump-dom", url],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout
        time.sleep(2)
        t = text_of(dom)
        og = re.search(r'property="og:image"\s+content="([^"]+)"', dom)
        title = re.search(r"<title>\s*(.*?)\s*(?:&ndash;|–)", dom, re.S)
        need = section(t, "You'll need:", ["Nutrition", "Step #1"])
        sauce_line = next((l for l in need.split("\n") if re.search(r"bay.?s kitchen", l, re.I)), "")
        uses = sorted({v for k, v in SAUCES.items() if re.search(k, sauce_line, re.I)})
        rec = {
            "url": url, "title": html.unescape(title.group(1)) if title else h,
            "image": og.group(1) if og else None,
            "prep": (re.search(r"(\d+)\s*mins?\s*\n\s*Prep time", t) or [None, None])[1],
            "cook": (re.search(r"(\d+)\s*mins?\s*\n\s*Cook time", t) or [None, None])[1],
            "serves": (re.search(r"(\d+)\s*\n\s*Serves", t) or [None, None])[1],
            "need": need, "nutrition": section(t, "Nutrition", ["Step #1\n"]),
            "method": section(t[t.find("Nutrition"):] if "Nutrition" in t else t, "Step #1", ["This recipe has been reviewed", "Copyright"]),
            "uses": uses,
        }
        out[h] = rec
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(("*" if uses else " "), h[:60], uses, rec["serves"], len(need))


if __name__ == "__main__":
    main()
