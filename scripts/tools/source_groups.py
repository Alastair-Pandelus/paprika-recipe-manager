"""Read a recipe page's ingredient list with its section headings.

  fetch_groups(url) -> [(heading, ingredient text), ...]

Works on the common WordPress recipe cards (Tasty Recipes, WP Recipe Maker)
and falls back to the HTML between an "Ingredients" heading and the
instructions. Heading is "" before the first section heading.
"""
from __future__ import annotations

import html
import re
import urllib.request

CONTAINERS = [
    ("tasty-recipes-ingredients-body", "tasty-recipes-instructions"),
    ("wprm-recipe-ingredients-container", "wprm-recipe-instructions-container"),
    ("mv-create-ingredients", "mv-create-instructions"),
]
TOKEN = re.compile(r"<(h[2-6]|li|strong|b|p)\b[^>]*>(.*?)</\1>", re.S | re.I)


def _text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(t)).strip(" ▢:")


def fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def ingredient_segment(page: str) -> str:
    for start, end in CONTAINERS:
        i = page.find(f'class="{start}')
        if i < 0:
            i = page.find(start + '"')
        if i >= 0:
            j = page.find(end, i)
            return page[i: j if j > i else i + 60000]
    m = re.search(r">\s*Ingredients\s*<", page, re.I)
    if not m:
        return ""
    rest = page[m.end():]
    n = re.search(r">\s*(?:Instructions|Method|Directions|Preparation)\s*<", rest, re.I)
    return rest[: n.start() if n else 60000]


def fetch_groups(url: str) -> list[tuple[str, str]]:
    seg = ingredient_segment(fetch_page(url))
    out: list[tuple[str, str]] = []
    heading = ""
    for tag, inner in TOKEN.findall(seg):
        text = _text(inner)
        if not text:
            continue
        if tag.lower() == "li":
            out.append((heading, text))
        elif "<li" not in inner.lower() and len(text) < 60:
            heading = text
    return out
