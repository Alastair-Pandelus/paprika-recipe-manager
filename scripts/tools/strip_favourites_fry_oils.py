"""Remove fry oils (olive / garlic-infused) from Favourites ingredients.

Fold into directions as fry wording so oil is not shopped.
Updates cloud API + local SQLite.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
FAV = "01A0427C-9963-4D6B-ACAF-5EA469E6ED1C"
APPLY = "--apply" in sys.argv

# Cooking/fry oils to drop from ingredients (keep sesame, oils in tins, etc.)
DROP_OIL_LINE = re.compile(
    r"(?i)^\s*("
    r"(?:\d+(?:[./]\d+)?|½|¼|¾|1½|1\s*½)\s*"
    r"(?:tsp|tbsp|teaspoon|tablespoon)s?\s+)?"
    r"(?:"
    r"garlic[- ]infused oil(?:\s*\([^)]*\))?|"
    r"garlic[- ]free oil(?:\s+or\s+olive oil)?|"
    r"olive oil"
    r")"
    r"(?:\s*,?\s*for frying)?\s*$"
)

# Also match without leading qty if somehow present
DROP_OIL_LOOSE = re.compile(
    r"(?i)^\s*(?:\d+(?:[./]\d+)?|½|¼|¾|1½|1\s*½)\s*(?:tsp|tbsp)s?\s+"
    r"(?:garlic[- ]infused oil|garlic[- ]free oil(?:\s+or\s+olive oil)?|olive oil)"
    r"(?:\s*\([^)]*\))?(?:\s*,?\s*for frying)?\s*$"
)


def is_drop_oil_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    # keep tin/packed products
    if re.search(r"(?i)\b(tin|tinned|can|packed|drained|sardines?)\b.*\boil\b|\bin olive oil\b", s):
        return False
    if re.search(r"(?i)sesame oil", s):
        return False
    return bool(DROP_OIL_LINE.match(s) or DROP_OIL_LOOSE.match(s))


def strip_oil_ingredients(ingredients: str) -> tuple[str, list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for ln in (ingredients or "").splitlines():
        if is_drop_oil_line(ln):
            dropped.append(ln.strip())
        else:
            kept.append(ln)
    # tidy double blank lines
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + ("\n" if kept else "")
    return text, dropped


def patch_directions(directions: str, dropped: list[str]) -> str:
    """Ensure fry steps mention oil without quantifying a shopping line."""
    d = directions or ""
    if not dropped:
        return d

    used_garlic = any(re.search(r"(?i)garlic", x) for x in dropped)
    oil_phrase = (
        "garlic-infused oil (or olive oil)"
        if used_garlic
        else "olive oil"
    )

    replacements = [
        (
            r"(?i)Heat the olive oil in a non-stick pan",
            f"Heat a little {oil_phrase} in a non-stick pan",
        ),
        (
            r"(?i)Heat the oil in a non-stick pan",
            f"Heat a little {oil_phrase} in a non-stick pan",
        ),
        (
            r"(?i)Heat the garlic-infused oil in a wok or large frying pan",
            f"Heat a little {oil_phrase} in a wok or large frying pan",
        ),
        (
            r"(?i)Heat the garlic-infused oil over",
            f"Heat a little {oil_phrase} over",
        ),
        (
            r"(?i)Heat garlic-infused oil over",
            f"Heat a little {oil_phrase} over",
        ),
        (
            r"(?i)Heat 1 tbsp garlic-infused oil in a wok or large frying pan",
            f"Heat a little {oil_phrase} in a wok or large frying pan",
        ),
        (
            r"(?i)Add a little more oil if needed",
            "Add a little more oil if needed",
        ),
    ]
    for pat, repl in replacements:
        d2 = re.sub(pat, repl, d)
        if d2 != d:
            d = d2

    # If directions still say "the oil" / "the garlic-infused oil" without fry phrasing
    d = re.sub(
        r"(?i)\bthe garlic-infused oil\b",
        f"a little {oil_phrase}",
        d,
    )
    return d


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def calc_hash(obj: dict) -> str:
    payload = dict(obj)
    payload.pop("hash", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    targets = []
    for cat in con.execute(
        "SELECT uid, name FROM recipe_categories WHERE parent_uid=?", (FAV,)
    ):
        for r in con.execute(
            """SELECT r.uid, r.name, r.ingredients, r.directions
               FROM recipes r
               JOIN recipes_to_categories rtc ON rtc.recipe_uid=r.uid
               WHERE rtc.category_uid=? AND coalesce(r.in_trash,0)=0""",
            (cat["uid"],),
        ):
            new_ings, dropped = strip_oil_ingredients(r["ingredients"] or "")
            if not dropped:
                continue
            new_dirs = patch_directions(r["directions"] or "", dropped)
            targets.append(
                {
                    "uid": r["uid"],
                    "name": r["name"],
                    "slot": cat["name"],
                    "dropped": dropped,
                    "ingredients": new_ings.rstrip("\n"),
                    "directions": new_dirs,
                }
            )

    safe_print(f"Favourites recipes to update: {len(targets)}")
    for t in targets:
        safe_print(f"  [{t['slot']}] {t['name']}")
        for d in t["dropped"]:
            safe_print(f"    drop: {d}")

    if not APPLY:
        safe_print("Dry-run. Pass --apply to write.")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        for t in targets:
            uid = t["uid"]
            await limiter.wait_turn()
            async with s.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            ) as resp:
                live = (await resp.json())["result"]
            live["ingredients"] = t["ingredients"].rstrip() + "\n"
            live["directions"] = t["directions"].rstrip() + "\n"
            live["hash"] = calc_hash(live)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(live),
                content_type="application/octet-stream",
                filename="data",
            )
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H, data=form
            ) as resp:
                txt = await resp.text()
                ok = '"result":true' in txt.replace(" ", "")
                safe_print(f"  API {t['name'][:48]}: {resp.status} ok={ok}")
                if not ok:
                    safe_print(f"    {txt[:300]}")
                    continue

            con.execute(
                "UPDATE recipes SET ingredients=?, directions=? WHERE uid=?",
                (t["ingredients"], t["directions"], uid),
            )
        con.commit()

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

    con.close()
    safe_print("Done")


if __name__ == "__main__":
    asyncio.run(main())
