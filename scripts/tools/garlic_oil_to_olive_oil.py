"""Replace garlic-infused oil / EVOO with "olive oil" across Paprika recipes.

Household preference: call the oil **olive oil** only — not garlic-infused oil
and not EVOO / extra virgin olive oil. Quantities are unchanged. Leaves
garlic-oil-making recipes themselves alone.

  python scripts/tools/garlic_oil_to_olive_oil.py
  python scripts/tools/garlic_oil_to_olive_oil.py --apply
  python scripts/tools/garlic_oil_to_olive_oil.py --apply --resume
"""
from __future__ import annotations

import asyncio
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
from normalize_ingredient_units import (  # noqa: E402
    RateLimiter,
    api_json,
    safe_print,
    save_recipe,
)

APPLY = "--apply" in sys.argv
PROGRESS = ROOT / "scripts" / "tools" / ".garlic_oil_to_olive_oil_progress.json"
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

FIELDS = ("name", "ingredients", "directions", "description", "notes")

# Oil-method recipes — keep as library how-tos; do not rewrite their contents.
SKIP_NAME_SUBSTR = (
    "garlic-infused olive oil (for low fodmap cooking)",
    "low fodmap garlic-infused oil",
    "sauce - garlic-infused olive oil",
)

RECIPE_LINK = re.compile(r"\[recipe:[^\]]*Garlic[^\]]*Oil[^\]]*\]", re.I)
# Longest phrases first → always "olive oil"
PHRASE_PATS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"extra[-\s]?virgin\s+olive\s+oil", re.I), "olive oil"),
    (re.compile(r"\bEVOO\b"), "olive oil"),
    (re.compile(r"garlic[-\s]?infused\s+olive\s+oil", re.I), "olive oil"),
    (re.compile(r"garlic[-\s]?infused\s+oil", re.I), "olive oil"),
    (re.compile(r"\bgarlic\s+oil\b", re.I), "olive oil"),
]

# Cleanup after swap / odd parenthetical forms
CLEANUPS: list[tuple[re.Pattern[str], str]] = [
    # "(Garlic-infused) olive oil" → olive oil
    (re.compile(r"\(\s*garlic[-\s]?infused\s*\)\s*olive oil", re.I), "olive oil"),
    # "olive oil (garlic-infused if you like)" / similar
    (
        re.compile(
            r"\bolive oil\s*\([^)]*garlic[-\s]?infused[^)]*\)",
            re.I,
        ),
        "olive oil",
    ),
    # "oil (garlic-infused if you like)"
    (
        re.compile(
            r"\boil\s*\([^)]*garlic[-\s]?infused[^)]*\)",
            re.I,
        ),
        "olive oil",
    ),
    (
        re.compile(
            r"\bolive oil\s*\(\s*olive oil\b[^)]*\)",
            re.I,
        ),
        "olive oil",
    ),
    # dangling LF garlic-replace notes on the same fragment
    (
        re.compile(
            r"\s*\(LF;\s*replaces\s+garlic[^)]*\)",
            re.I,
        ),
        "",
    ),
    (
        re.compile(
            r"\s*\(optional for garlic flavour[^)]*\)",
            re.I,
        ),
        "",
    ),
    (
        re.compile(
            r"\s*\(for garlic flavour[^)]*\)",
            re.I,
        ),
        "",
    ),
    # "olive oil / olive oil" collapse
    (re.compile(r"\bolive oil\s*/\s*olive oil\b", re.I), "olive oil"),
    (re.compile(r"\bolive oil\s+or\s+olive oil\b", re.I), "olive oil"),
]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def match_case(sample: str, replacement: str) -> str:
    if sample.isupper():
        return replacement.upper()
    if sample[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def should_skip(name: str) -> bool:
    n = (name or "").lower()
    # strip leading emoji markers for match
    for ch in ("✅", "ℹ️", "❌", "♡", "❣", "💔", "♥", "💚"):
        n = n.replace(ch, "")
    n = " ".join(n.split()).strip(" -")
    return any(s in n for s in SKIP_NAME_SUBSTR)


def _has_oil_target(text: str) -> bool:
    if RECIPE_LINK.search(text):
        return True
    for pat, _ in PHRASE_PATS:
        if pat.search(text):
            return True
    # Parenthetical / optional garlic-infused wording
    if re.search(r"garlic[-\s]?infused", text, re.I):
        return True
    if re.search(r"\bgarlic\s+oil\b", text, re.I):
        return True
    return False


def convert_text(text: str) -> str | None:
    """Rewrite only when garlic-infused / EVOO oil wording is present."""
    if not text or not _has_oil_target(text):
        return None
    original = text

    text = RECIPE_LINK.sub("olive oil", text)

    for pat, repl in PHRASE_PATS:
        # Always prefer lowercase "olive oil" (never OLIVE OIL from EVOO)
        text = pat.sub(repl, text)

    for pat, repl in CLEANUPS:
        text = pat.sub(repl, text)

    # Collapse doubled oil after garlic→olive on a line that already had olive oil
    text = re.sub(r"\bolive oil,\s*olive oil\b", "olive oil", text, flags=re.I)
    text = re.sub(r"\bolive oil\s+and\s+olive oil\b", "olive oil", text, flags=re.I)
    text = re.sub(r"\ba olive oil\b", "an olive oil", text, flags=re.I)
    # Catch any leftover "garlic-infused … oil" wording
    text = re.sub(
        r"garlic[-\s]?infused(?:\s+olive)?\s+oil",
        "olive oil",
        text,
        flags=re.I,
    )

    # tidy double spaces left by cleanup (keep newlines) — only on changed text
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    text = re.sub(r" +([,.;:])", r"\1", text)

    if text == original:
        return None
    return text

def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    if should_skip(rec.get("name") or ""):
        return None, []
    changes: list[str] = []
    out = dict(rec)
    for field in FIELDS:
        old = rec.get(field) or ""
        new = convert_text(old)
        if new is None:
            continue
        out[field] = new
        if field == "name":
            changes.append(f"name: {old} => {new}")
        else:
            shown = False
            for a, b in zip(old.splitlines(), new.splitlines()):
                if a != b:
                    changes.append(f"{field}: {a.strip()[:100]} => {b.strip()[:100]}")
                    shown = True
                    break
            if not shown:
                changes.append(f"{field}: updated")
    if not changes:
        return None, []
    return out, changes


def update_local(uid: str, rec: dict) -> None:
    if not LOCAL_DB.exists():
        return
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        """
        UPDATE recipes
        SET name=?, ingredients=?, directions=?, description=?, notes=?,
            status='modified', is_synced=0
        WHERE uid=?
        """,
        (
            rec.get("name") or "",
            rec.get("ingredients") or "",
            rec.get("directions") or "",
            rec.get("description") or "",
            rec.get("notes") or "",
            uid,
        ),
    )
    con.commit()
    con.close()


async def main() -> None:
    demos = [
        "1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]  ♥ Sat fat 0.8g",
        "By the hob (no bowl): garlic-infused olive oil, salt.",
        "Heat garlic-infused oil in a pan.",
        "extra virgin olive oil ([recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)] optional for garlic flavour)",
        "1 tbsp EVOO",
        "1 tsp garlic oil",
        "Garlic-Infused Olive Oil (For Low FODMAP Cooking)",  # title of oil recipe — skipped at recipe level
    ]
    safe_print("Demos:")
    for d in demos:
        safe_print(f"  {d!r}")
        safe_print(f"  => {convert_text(d)!r}")

    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
    elif PROGRESS.exists():
        PROGRESS.unlink()

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
    changed = saved = unchanged = skipped = failed = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers
        )
        index = body["result"]
        safe_print(f"Index={len(index)} MODE={'APPLY' if APPLY else 'DRY'}")

        for i, e in enumerate(index, 1):
            uid = e["uid"]
            if uid in done:
                continue
            st, body = await api_json(
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec or rec.get("in_trash"):
                done.add(uid)
                continue

            if should_skip(rec.get("name") or ""):
                skipped += 1
                done.add(uid)
                continue

            fixed, changes = fix_recipe(rec)
            if not fixed:
                unchanged += 1
            else:
                changed += 1
                name = rec.get("name") or uid
                if changed <= 25 or APPLY:
                    safe_print(f"FIX [{changed}] {name}")
                    for c in changes[:4]:
                        safe_print(f"  • {c}")
                if APPLY:
                    fixed["hash"] = calc_hash(fixed)
                    if await save_recipe(s, limiter, headers, fixed):
                        saved += 1
                        update_local(uid, fixed)
                    else:
                        failed += 1
                        safe_print(f"FAIL {name}")

            done.add(uid)
            if i % 50 == 0:
                PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
                safe_print(
                    f"… {i}/{len(index)} changed={changed} saved={saved} "
                    f"unchanged={unchanged} skipped={skipped} fail={failed}"
                )

        if APPLY and saved:
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()
        PROGRESS.unlink(missing_ok=True)

    safe_print(
        f"Done. changed={changed} saved={saved} unchanged={unchanged} "
        f"skipped_oil_recipes={skipped} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
