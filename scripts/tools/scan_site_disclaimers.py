"""Read-only scan of recipe websites for "not low FODMAP" style disclaimers.

For every recipe in the Low Fodmap folder tree with a source URL, fetch the
page (about 1 request per second per site) and record sentences that say the
recipe is not suitable / not low FODMAP / retested / "if you tolerate", or
mention high FODMAP. Writes docs/site-disclaimers.tsv. Changes nothing.

  python scripts/tools/scan_site_disclaimers.py [--only "name fragment" ...]
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from cleanup_library import LOCAL_DB  # noqa: E402
from source_groups import fetch_page  # noqa: E402

PHRASES = [
    ("not suitable for the elimination phase", re.compile(r"not\s+suitable\s+for\s+the\s+elimination", re.I)),
    ("not low FODMAP", re.compile(r"not\s+(?:a\s+)?low[-\s]fodmap", re.I)),
    ("retested", re.compile(r"\bre-?tested\b", re.I)),
    ("if you tolerate", re.compile(r"if\s+you\s+(?:can\s+)?tolerate", re.I)),
    ("not suitable for", re.compile(r"not\s+suitable\s+for", re.I)),
    ("high FODMAP", re.compile(r"high[-\s]fodmap", re.I)),
    # A low FODMAP variant the site describes, which Paprika may not reflect.
    ("variant: can be made low FODMAP", re.compile(r"(?:can|could)\s+(?:also\s+)?(?:easily\s+)?be\s+made\s+low[-\s]fodmap", re.I)),
    ("variant: to make it low FODMAP", re.compile(r"to\s+make\s+(?:this|it)\s+(?:recipe\s+)?low[-\s]fodmap", re.I)),
    ("variant: for low FODMAP use/swap/omit", re.compile(r"for\s+(?:a\s+)?low[-\s]fodmap\s*(?:version|variation)?[,:]?\s+(?:use|swap|omit|skip|replace|leave)", re.I)),
    ("variant: low FODMAP note/variation", re.compile(r"low[-\s]fodmap\s+(?:notes?|variation|version|option|swap)", re.I)),
]
# Site chrome and comments are not the recipe's own advice.
CUT = re.compile(r"(?:\bLeave a (?:Reply|Comment)\b|\d+ Comments\b|\bComments\b\s*\d|Reader Interactions)", re.I)


def page_text(url: str) -> str:
    p = fetch_page(url)
    p = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", p, flags=re.S | re.I)
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", p)))
    m = CUT.search(t)
    return t[: m.start()] if m else t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--out", default=str(ROOT / "docs" / "site-disclaimers.tsv"))
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    cats = {u: (n, p) for u, n, p in con.execute("select uid, name, parent_uid from recipe_categories")}

    def root(cu):
        n, p = cats[cu]
        while p in cats:
            n, p = cats[p]
        return n

    recipes = []
    for uid, name, src, url in con.execute(
        "select uid, name, coalesce(source,''), coalesce(source_url,'') from recipes "
        "where coalesce(in_trash,0)=0 and source_url like 'http%'"
    ):
        cus = [c for (c,) in con.execute(
            "select category_uid from recipes_to_categories where recipe_uid=?", (uid,)) if c in cats]
        if not any(root(c).lower().startswith("low fodmap") for c in cus):
            continue
        if args.only and not any(o.lower() in name.lower() for o in args.only):
            continue
        recipes.append((name, src, url))
    con.close()

    last: dict[str, float] = {}
    rows, failed = [], 0
    for i, (name, src, url) in enumerate(recipes, 1):
        host = urlparse(url).netloc
        wait = 1.0 - (time.time() - last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        last[host] = time.time()
        try:
            text = page_text(url)
        except Exception as e:  # noqa: BLE001
            failed += 1
            rows.append([name, src, "", f"fetch failed: {str(e)[:60]}", name.split(" ")[0], url])
            continue
        seen = set()
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            for label, pat in PHRASES:
                if pat.search(sentence) and len(sentence) < 400 and sentence not in seen:
                    seen.add(sentence)
                    rows.append([name, src, label, sentence.strip(), name.split(" ")[0], url])
                    break
        if i % 50 == 0:
            print(f"{i}/{len(recipes)} pages", flush=True)

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["recipe", "source", "phrase", "quoted sentence", "current marker", "url"])
        w.writerows(rows)
    hits = [r for r in rows if r[2]]
    print(f"pages {len(recipes)}, failed {failed}, sentences {len(hits)}, recipes {len({r[0] for r in hits})}")


if __name__ == "__main__":
    main()
