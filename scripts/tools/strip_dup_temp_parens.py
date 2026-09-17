"""
Remove duplicate temperature parentheses after Celsius standardization.

Examples:
  Pre-heat the oven to 200°C (200°C).  ->  Pre-heat the oven to 200°C.
  Bake at 180°C (180°C) for 20 min.   ->  Bake at 180°C for 20 min.
  220°C (425°F) if any F left         ->  220°C
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
PROGRESS = ROOT / "scripts" / "tools" / ".strip_dup_temp_parens_progress.json"

# 180°C (180°C) / 180°C (180 C) / 180 °C (180 degrees C)
RE_DUP_C = re.compile(
    r"(?i)"
    r"(?P<keep>\d{2,3})\s*°\s*C"
    r"\s*\(\s*(?P=keep)\s*(?:°\s*)?C(?:elsius|entigrade)?\s*"
    r"(?:degrees?)?\s*\)"
)

# Also: 180°C (180 degrees Celsius) with word order variations
RE_DUP_C_LONG = re.compile(
    r"(?i)"
    r"(?P<keep>\d{2,3})\s*°\s*C"
    r"\s*\(\s*(?P=keep)\s*degrees?\s*C(?:elsius|entigrade)?\s*\)"
)

# Leftover F in parens after a °C: 220°C (425°F) or 220°C (425 F)
RE_C_THEN_F = re.compile(
    r"(?i)"
    r"(?P<keep>\d{2,3})\s*°\s*C"
    r"\s*\(\s*\d{2,3}\s*(?:°\s*)?F(?:ahrenheit)?\s*\)"
)

# Near-duplicate within ±5°C from conversion drift: 190°C (195°C)
RE_NEAR_DUP_C = re.compile(
    r"(?i)"
    r"(?P<keep>\d{2,3})\s*°\s*C"
    r"\s*\(\s*(?P<inner>\d{2,3})\s*(?:°\s*)?C(?:elsius|entigrade)?\s*\)"
)

FIELDS = ("directions", "description", "notes")


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def strip_dup_temps(text: str) -> str | None:
    if not text:
        return None
    original = text

    text = RE_DUP_C.sub(lambda m: f"{m.group('keep')}°C", text)
    text = RE_DUP_C_LONG.sub(lambda m: f"{m.group('keep')}°C", text)
    text = RE_C_THEN_F.sub(lambda m: f"{m.group('keep')}°C", text)

    def near(m: re.Match[str]) -> str:
        keep = int(m.group("keep"))
        inner = int(m.group("inner"))
        if abs(keep - inner) <= 5:
            return f"{keep}°C"
        return m.group(0)

    text = RE_NEAR_DUP_C.sub(near, text)

    # tidy spaces before punctuation: "200°C ."
    text = re.sub(r"°C\s+\.", "°C.", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    if text == original:
        return None
    return text


def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    changes: list[str] = []
    out = dict(rec)
    for field in FIELDS:
        old = rec.get(field) or ""
        new = strip_dup_temps(old)
        if new is None:
            continue
        out[field] = new
        for a, b in zip(old.splitlines(), new.splitlines()):
            if a != b:
                changes.append(f"{field}: {a.strip()[:100]} => {b.strip()[:100]}")
                break
    if not changes:
        return None, []
    return out, changes


async def main() -> None:
    demos = [
        "Pre-heat the oven to 200°C (200°C).",
        "Bake at 180°C (180 C) for 20 minutes.",
        "Preheat oven to 220°C (425°F).",
        "Roast at 190°C (195°C) until done.",
        "Preheat oven to 200°C (fan 180°C).",  # keep — not a duplicate unit
    ]
    safe_print("Demos:")
    for d in demos:
        safe_print(f"  {d!r} => {strip_dup_temps(d)!r}")

    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
    elif PROGRESS.exists():
        PROGRESS.unlink()

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.45)
    changed = saved = unchanged = failed = 0
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
        f"Done. changed={changed} saved={saved} unchanged={unchanged} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
