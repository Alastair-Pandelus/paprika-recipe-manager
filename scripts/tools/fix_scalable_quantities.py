"""
Rewrite ingredient lines Paprika cannot scale into scalable forms.

Examples:
  One 14.5 oz can coconut milk  ->  1 (14.5 oz) can coconut milk
  14 oz can diced tomatoes      ->  1 (14 oz) can diced tomatoes
  Two 16-oz packages spinach    ->  2 (16 oz) packages spinach
  one tbsp butter               ->  1 tbsp butter
  A cup of yoghurt              ->  1 cup yoghurt
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
PROGRESS = ROOT / "scripts" / "tools" / ".fix_scalable_quantities_progress.json"

WORD_TO_NUM = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "a": "1",
    "an": "1",
}

# One 14.5 oz can X  /  One 14.5-oz can X
RE_WORD_SIZE_CAN = re.compile(
    r"(?i)^\s*(one|two|three|four|five|six|seven|eight|nine|ten|a|an)\s+"
    r"(\d+(?:\.\d+)?)\s*-?\s*(oz|ounce|ounces|g|gram|grams|ml|lb|pound|pounds)\s+"
    r"(cans?|packages?|pkgs?|jars?|tins?|bottles?|boxes?)\b"
    r"(?:\s+of)?\s*(.*)$"
)

# 14 oz can X  (no leading count)
RE_BARE_SIZE_CAN = re.compile(
    r"(?i)^\s*(\d+(?:\.\d+)?)\s*-?\s*(oz|ounce|ounces|g|gram|grams|ml|lb|pound|pounds)\s+"
    r"(cans?|packages?|pkgs?|jars?|tins?|bottles?|boxes?)\b"
    r"(?:\s+of)?\s*(.*)$"
)

# Two 16-oz packages of X (already covered by RE_WORD_SIZE_CAN)
# One package of brie / One can of tuna
RE_WORD_CONTAINER = re.compile(
    r"(?i)^\s*(one|two|three|four|five|six|seven|eight|nine|ten|a|an)\s+"
    r"(cans?|packages?|pkgs?|jars?|tins?|bottles?|boxes?)\s+"
    r"(?:of\s+)?(.*)$"
)

# Leading word-number before a unit: one tbsp / A cup
RE_WORD_UNIT = re.compile(
    r"(?i)^\s*(one|two|three|four|five|six|seven|eight|nine|ten|a|an)\s+"
    r"(tbsp|tsp|tablespoons?|teaspoons?|cups?|g|kg|ml|oz|lb|pounds?|ounces?|"
    r"cloves?|slices?|pieces?|handfuls?|pinches?)\b(.*)$"
)

UNIT_SHORT = {
    "ounce": "oz",
    "ounces": "oz",
    "gram": "g",
    "grams": "g",
    "pound": "lb",
    "pounds": "lb",
}


def short_unit(u: str) -> str:
    return UNIT_SHORT.get(u.lower(), u.lower())


def fix_line(line: str) -> str:
    raw = line
    # keep section headers
    if re.fullmatch(r"\s*[^:\n]+:\s*", line):
        return line

    m = RE_WORD_SIZE_CAN.match(line)
    if m:
        n = WORD_TO_NUM[m.group(1).lower()]
        size, unit, container, rest = m.group(2), short_unit(m.group(3)), m.group(4).lower(), m.group(5).strip()
        # singular container after count when n==1 often stays as typed; keep original pluralization lightly
        return f"{n} ({size} {unit}) {container} {rest}".rstrip()

    m = RE_BARE_SIZE_CAN.match(line)
    if m:
        size, unit, container, rest = m.group(1), short_unit(m.group(2)), m.group(3).lower(), m.group(4).strip()
        return f"1 ({size} {unit}) {container} {rest}".rstrip()

    m = RE_WORD_CONTAINER.match(line)
    if m:
        n = WORD_TO_NUM[m.group(1).lower()]
        container, rest = m.group(2).lower(), m.group(3).strip()
        return f"{n} {container} {rest}".rstrip()

    m = RE_WORD_UNIT.match(line)
    if m:
        n = WORD_TO_NUM[m.group(1).lower()]
        unit, rest = m.group(2), m.group(3)
        # normalize long unit words lightly
        ul = unit.lower()
        if ul.startswith("tablespoon"):
            unit = "tbsp"
        elif ul.startswith("teaspoon"):
            unit = "tsp"
        elif ul.startswith("cup"):
            unit = "cup" if not ul.endswith("s") else "cups"
        return f"{n} {unit}{rest}"

    return raw


def fix_ingredients(text: str) -> str | None:
    if not text:
        return None
    lines = text.splitlines()
    out = [fix_line(L) for L in lines]
    if out == lines:
        return None
    # preserve trailing newline if original had one
    joined = "\n".join(out)
    if text.endswith("\n"):
        joined += "\n"
    return joined


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def main() -> None:
    # demos
    demos = [
        "One 14.5 oz can full-fat coconut milk",
        "14 oz can diced tomatoes",
        "14oz can fire roasted tomatoes",
        "Two 16-oz packages of frozen spinach (thawed)",
        "One can of tuna in water",
        "one tbsp butter",
        "A cup of lactose-free sour cream",
        "1 can of tomato paste",
        "3 large eggs",
    ]
    safe_print("Demos:")
    for d in demos:
        safe_print(f"  {d!r} -> {fix_line(d)!r}")

    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
        safe_print(f"Resuming; {len(done)} done")
    elif PROGRESS.exists():
        PROGRESS.unlink()

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.45)
    changed = saved = unchanged = failed = 0
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s, limiter, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw}
        )
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        st, body = await api_json(s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers)
        index = body["result"]
        safe_print(f"Index={len(index)} MODE={'APPLY' if APPLY else 'DRY'}")

        for i, e in enumerate(index, 1):
            uid = e["uid"]
            if uid in done:
                continue
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
            )
            rec = (body or {}).get("result") or {}
            if not rec:
                done.add(uid)
                continue
            ings = rec.get("ingredients") or ""
            fixed = fix_ingredients(ings)
            if not fixed:
                unchanged += 1
            else:
                changed += 1
                name = rec.get("name") or uid
                # show first differing line
                for a, b in zip(ings.splitlines(), fixed.splitlines()):
                    if a != b:
                        safe_print(f"FIX [{changed}] {name}")
                        safe_print(f"  {a}  =>  {b}")
                        break
                if APPLY:
                    rec["ingredients"] = fixed
                    rec["hash"] = calc_hash(rec)
                    if await save_recipe(s, limiter, headers, rec):
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
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
                await r.text()
        PROGRESS.unlink(missing_ok=True)

    safe_print(
        f"Done. changed={changed} saved={saved} unchanged={unchanged} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
