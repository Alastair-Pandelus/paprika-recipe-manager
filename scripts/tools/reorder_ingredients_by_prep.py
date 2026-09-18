"""Reorder Paprika recipe ingredients to match Prep (bowls) + hob use order.

Matches each ingredient line to the earliest Prep bowl / hob section that
mentions it, then emits lines in section order. Unmatched lines keep relative
order at the end (before trailing oil/salt if those were unmatched).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
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
MAIN_MEALS = "9754B607-D3B4-46BB-9289-ADE50C4F098E"
FIELD_DOCTOR = "DFD7AB80-DB1E-4257-A6E9-7E9AA44E548C"

STOP = {
    "the",
    "and",
    "with",
    "from",
    "into",
    "onto",
    "for",
    "a",
    "an",
    "of",
    "or",
    "to",
    "in",
    "on",
    "at",
    "as",
    "if",
    "be",
    "is",
    "are",
    "have",
    "has",
    "ready",
    "beside",
    "this",
    "bowl",
    "mix",
    "chop",
    "chopped",
    "dice",
    "diced",
    "slice",
    "sliced",
    "peel",
    "peeled",
    "grate",
    "mince",
    "rinse",
    "pat",
    "dry",
    "wash",
    "roughly",
    "finely",
    "optional",
    "remaining",
    "half",
    "rest",
    "reserve",
    "set",
    "out",
    "small",
    "dish",
    "large",
    "medium",
    "warm",
    "heat",
    "cook",
    "until",
    "tender",
    "fresh",
    "ground",
    "low",
    "fodmap",
    "extra",
    "virgin",
    "plus",
    "more",
    "divided",
    "serving",
    "wedges",
}


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def tokens(text: str) -> set[str]:
    text = text.lower()
    text = re.sub(r"\[recipe:[^\]]+\]", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    words = [w for w in re.split(r"[\s\-]+", text) if len(w) > 2 and w not in STOP]
    return set(words)


def ingredient_core(line: str) -> str:
    """Strip leading quantity for matching."""
    s = line.strip()
    s = re.sub(
        r"^(?:about\s+)?(?:\d[\d./\s]*\s*(?:to|-|–)\s*)?\d[\d./\s]*\s*"
        r"(?:g|kg|ml|l|litres?|cups?|tbsp|tsp|oz|lb|lbs|x)?\s*",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"^(?:pinch of|sprinkle of|handful|zest of|juice of)\s+", "", s, flags=re.I)
    return s


def parse_sections(directions: str) -> list[tuple[str, str]]:
    """Return ordered (section_key, body_text) from Prep bowls + hob."""
    d = directions or ""
    if "Prep (bowls)" not in d:
        return []
    # Cut cook steps (numbered) off
    prep = re.split(r"\n(?=\d+\.\s)", d, maxsplit=1)[0]
    sections: list[tuple[str, str]] = []
    # Bowl blocks
    for m in re.finditer(
        r"(?im)^((?:Large|Medium|Small) bowl\s*[—–-]\s*[^\n:]+):\s*\n((?:[ \t]*[-•].*\n?)*)",
        prep,
    ):
        title = m.group(1).strip()
        body = m.group(2)
        key = title.lower()
        sections.append((key, title + "\n" + body))
    # Hob
    hm = re.search(r"(?im)^By the hob[^\n]*:\s*(.+?)\s*$", prep)
    if hm:
        sections.append(("by the hob", "By the hob: " + hm.group(1)))
    return sections

def is_finish_section(key: str) -> bool:
    return "finish" in key


def is_hob_section(key: str) -> bool:
    return "hob" in key


def score_line_to_section(line: str, section_key: str, section_text: str) -> float:
    lt = tokens(ingredient_core(line) + " " + line)
    st = tokens(section_text)
    if not lt or not st:
        return 0.0
    overlap = lt & st
    if not overlap:
        for a in list(lt):
            if a.endswith("s") and a[:-1] in st:
                overlap.add(a)
            elif (a + "s") in st:
                overlap.add(a)
        if not overlap:
            return 0.0
    score = len(overlap) / max(len(lt), 1) + 0.15 * len(overlap)

    low = line.lower()
    # Liquids belong on the hob, not in a meat/veg bowl that shares a word
    # (e.g. "chicken stock" must not win the chicken bowl).
    liquidish = bool(
        re.search(
            r"(?i)\b(stock|broth|wine|milk|cream|juice|oil|water|tamari|soy sauce|"
            r"vinegar|sauce)\b",
            low,
        )
    )
    if liquidish and not is_hob_section(section_key):
        score *= 0.15
    if liquidish and is_hob_section(section_key):
        score += 0.8

    # Fresh garnish herbs: prefer finish section
    if re.search(
        r"(?i)\b(parsley|basil|mint)\b|fresh coriander|fresh cilantro|sesame seeds",
        low,
    ) and not re.search(r"(?i)ground coriander|ground cumin", low):
        if is_finish_section(section_key):
            score += 1.2
        elif "spice" in section_key:
            score *= 0.4

    return score


def reorder_ingredients(ingredients: str, directions: str) -> tuple[str, bool]:
    lines = [ln.rstrip() for ln in (ingredients or "").splitlines()]
    nonempty = [(i, ln) for i, ln in enumerate(lines) if ln.strip()]
    if not nonempty:
        return ingredients or "", False

    sections = parse_sections(directions)
    if not sections:
        return ingredients or "", False

    FINISH_BIAS = re.compile(
        r"(?i)\b(parsley|basil|mint|coriander|cilantro|chives?|sesame seeds|"
        r"to garnish|for garnish|to finish|lemon wedges|lime wedges)\b"
    )
    assignments: dict[int, int] = {}
    for li, ln in nonempty:
        scores = [
            score_line_to_section(ln, key, body) for key, body in sections
        ]
        if FINISH_BIAS.search(ln) and not re.search(
            r"(?i)ground coriander|ground cumin", ln
        ):
            for si, (key, _) in enumerate(sections):
                if is_finish_section(key):
                    scores[si] += 1.5
        best = max(range(len(scores)), key=lambda i: (scores[i], -i))
        # Weak meat/veg hits for liquids → hob
        if (
            scores[best] < 0.35
            and re.search(
                r"(?i)\b(stock|broth|wine|milk|oil|juice|tamari|soy sauce|water|salt|pepper|tomato)\b",
                ln,
            )
        ):
            for si, (key, _) in enumerate(sections):
                if is_hob_section(key):
                    best = si
                    scores[best] = max(scores[best], 1.0)
                    break
        if scores[best] <= 0:
            if re.search(
                r"(?i)\b(stock|broth|wine|milk|oil|juice|tamari|soy sauce|water|salt|pepper)\b",
                ln,
            ):
                for si, (key, _) in enumerate(sections):
                    if is_hob_section(key):
                        best = si
                        break
                else:
                    best = -1
            else:
                best = -1
            if best == -1:
                assignments[li] = 10_000
                continue
        assignments[li] = best

    # Ingredient list order: Prep bowls as written, but finish/garnish AFTER hob
    # (hob liquids are used before final garnish even if Prep lists finish above hob).
    def sort_key(section_idx: int) -> tuple:
        if section_idx >= 10_000:
            return (10_000, 0)
        key = sections[section_idx][0]
        if is_finish_section(key):
            return (5_000, section_idx)  # after normal bowls + hob
        if is_hob_section(key):
            return (4_000, section_idx)
        return (section_idx, 0)

    ordered_idx = sorted(
        assignments.keys(), key=lambda i: (sort_key(assignments[i]), i)
    )
    new_nonempty = [lines[i] for i in ordered_idx]
    new_text = "\n".join(new_nonempty).rstrip() + "\n"
    old_text = "\n".join(ln for ln in lines if ln.strip()).rstrip() + "\n"
    return new_text, new_text != old_text

async def post_recipe(session, headers, recipe: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(recipe),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
        headers=headers,
        data=form,
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


async def main() -> None:
    dry = "--dry-run" in sys.argv
    cat = FIELD_DOCTOR if "--field-doctor" in sys.argv else MAIN_MEALS
    cat_label = "Field Doctor" if cat == FIELD_DOCTOR else "Main Meals"
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT r.uid, r.name, r.ingredients, r.directions
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        WHERE rtc.category_uid = ? AND r.in_trash = 0
          AND (r.directions LIKE 'Prep (bowls)%'
               OR r.directions LIKE '**Prep (bowls)**%'
               OR r.directions LIKE '**Prep (bowls):**%')
        ORDER BY r.name COLLATE NOCASE
        """,
        (cat,),
    ).fetchall()

    updates: list[tuple[str, str, str, str]] = []  # uid, name, old, new
    for r in rows:
        new_ing, changed = reorder_ingredients(r["ingredients"] or "", r["directions"] or "")
        if changed:
            updates.append((r["uid"], r["name"], r["ingredients"] or "", new_ing))

    safe_print(f"{cat_label} with Prep bowls: {len(rows)}")
    safe_print(f"Ingredient order changes: {len(updates)}")

    # Show paella + a few samples
    for uid, name, old, new in updates:
        if "Paella" in name and "Gluten-Free" in name:
            safe_print(f"\nSAMPLE {name}")
            safe_print(" WAS: " + " | ".join(old.splitlines()[:6]))
            safe_print(" NOW: " + " | ".join(new.splitlines()[:8]))
            break
    for uid, name, old, new in updates[:3]:
        safe_print(f"\n{name}")
        safe_print(" first was: " + (old.splitlines()[0] if old.strip() else ""))
        safe_print(" first now: " + (new.splitlines()[0] if new.strip() else ""))

    if dry:
        safe_print("\nDry-run only; no writes.")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.25)
    ok_n = fail_n = 0
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}
        for i, (uid, name, _old, new_ing) in enumerate(updates, 1):
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            rec = body.get("result") or {}
            if not rec.get("uid"):
                fail_n += 1
                continue
            rec["ingredients"] = new_ing
            rec["hash"] = calc_hash(rec)
            await limiter.wait_turn()
            ok = await post_recipe(s, H, rec)
            if ok:
                ok_n += 1
                con.execute(
                    "UPDATE recipes SET ingredients=?, status=? WHERE uid=?",
                    (new_ing, "unmodified", uid),
                )
            else:
                fail_n += 1
            if i % 50 == 0 or i == len(updates):
                safe_print(f"progress {i}/{len(updates)} ok={ok_n} fail={fail_n}")
                con.commit()
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")
    con.commit()
    con.close()
    safe_print(f"done ok={ok_n} fail={fail_n}")


if __name__ == "__main__":
    asyncio.run(main())
