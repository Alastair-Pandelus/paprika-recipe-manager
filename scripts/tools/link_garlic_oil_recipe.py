"""
Add Paprika [recipe:…] links to Garlic-Infused Olive Oil from recipes
that use homemade garlic-infused oil (not Fody bottled oil).

Target: Garlic-Infused Olive Oil (For Low FODMAP Cooking)
Link format: [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
PROGRESS = ROOT / "scripts" / "tools" / ".link_garlic_oil_progress.json"

TARGET_NAME = "Garlic-Infused Olive Oil (For Low FODMAP Cooking)"
TARGET_UID = "A3AD7742-5E2E-4737-A1F1-F62E78565DB8"
RECIPE_LINK = f"[recipe:{TARGET_NAME}]"

# Other oil-howto recipes — don't link from these to themselves
SKIP_UIDS = {
    TARGET_UID,
    "E562428D-B9E9-4650-AA66-5BE2D317B59A",  # Low FODMAP garlic-infused oil
    "E85A6B3C-C15F-41DE-BC34-DEFC76C45DD3",  # Sauce - Garlic-Infused Olive Oil
}

# Homemade-style mentions (not brand product)
OIL_PHRASE = re.compile(
    r"(?i)\bgarlic[\s-]*infused(?:\s+olive)?\s+oils?\b"
)
FODY = re.compile(r"(?i)\bfody")
ALREADY_LINKED = re.compile(
    re.escape(f"[recipe:{TARGET_NAME}]")
    + r"|"
    + re.escape("[recipe:Sauce - Garlic-Infused Olive Oil]")
    + r"|"
    + re.escape("[recipe:Low FODMAP garlic-infused oil]"),
    re.I,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def line_is_fody_product(line: str) -> bool:
    """True if this line is about Fody's bottled oil, not homemade."""
    if not FODY.search(line):
        return False
    # Fody appears on same line as the oil phrase
    if OIL_PHRASE.search(line):
        return True
    return False


def link_phrase_once(text: str) -> tuple[str, bool]:
    """Replace first non-Fody garlic-infused oil phrase with recipe link."""
    if ALREADY_LINKED.search(text):
        return text, False
    if not OIL_PHRASE.search(text):
        return text, False

    # Work line-by-line so we can skip Fody lines
    lines = text.splitlines(keepends=True)
    changed = False
    out: list[str] = []
    for line in lines:
        if changed:
            out.append(line)
            continue
        # strip keepends for checks
        core = line.rstrip("\r\n")
        ending = line[len(core) :]
        if line_is_fody_product(core) or not OIL_PHRASE.search(core):
            out.append(line)
            continue
        new_core = OIL_PHRASE.sub(RECIPE_LINK, core, count=1)
        if new_core != core:
            changed = True
            out.append(new_core + ending)
        else:
            out.append(line)
    return "".join(out), changed


def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    uid = rec.get("uid") or ""
    if uid in SKIP_UIDS:
        return None, []

    changes: list[str] = []
    out = dict(rec)

    ings = rec.get("ingredients") or ""
    dirs = rec.get("directions") or ""

    new_ings, ings_changed = link_phrase_once(ings)
    if ings_changed:
        out["ingredients"] = new_ings
        for a, b in zip(ings.splitlines(), new_ings.splitlines()):
            if a != b:
                changes.append(f"ingredients: {a.strip()[:100]} => {b.strip()[:100]}")
                break
    else:
        # Fall back to directions if oil only mentioned there
        new_dirs, dirs_changed = link_phrase_once(dirs)
        if dirs_changed:
            out["directions"] = new_dirs
            for a, b in zip(dirs.splitlines(), new_dirs.splitlines()):
                if a != b:
                    changes.append(
                        f"directions: {a.strip()[:100]} => {b.strip()[:100]}"
                    )
                    break

    if not changes:
        return None, []
    return out, changes


async def main() -> None:
    demos = [
        "2 tbsp garlic-infused olive oil",
        "1–2 tbsp garlic-infused olive oil (LF; replaces onion/garlic)",
        "2 tbsp Fody’s Garlic-Infused Olive Oil",
        "3 tbsp Fody Garlic-Infused Olive Oil, divided",
        f"2 tbsp {RECIPE_LINK}",
    ]
    safe_print("Demos:")
    for d in demos:
        linked, ch = link_phrase_once(d)
        safe_print(f"  changed={ch} {d!r} => {linked!r}")

    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
    elif PROGRESS.exists():
        PROGRESS.unlink()

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.45)
    changed = saved = unchanged = failed = skipped = 0
    async with aiohttp.ClientSession() as s:
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
        safe_print(f"Link target: {TARGET_NAME}")

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
            if uid in SKIP_UIDS:
                skipped += 1
                done.add(uid)
                continue

            fixed, changes = fix_recipe(rec)
            if not fixed:
                unchanged += 1
            else:
                changed += 1
                name = rec.get("name") or uid
                safe_print(f"FIX [{changed}] {name}")
                for c in changes[:2]:
                    safe_print(f"  • {c}")
                if APPLY:
                    fixed["hash"] = calc_hash(fixed)
                    if await save_recipe(s, limiter, headers, fixed):
                        saved += 1
                    else:
                        failed += 1
                        safe_print(f"FAIL {name}")

            done.add(uid)
            if i % 50 == 0:
                PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
                safe_print(
                    f"… {i}/{len(index)} changed={changed} saved={saved} "
                    f"unchanged={unchanged} fail={failed}"
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
