"""
Standardize oven (and related) temperatures to Celsius (°C).

- Explicit °F / F / degrees F -> °C (nearest 5)
- Dual F/C -> keep °C only (prefer converted F rounded to nearest 5)
- Bare "Preheat oven to 425" / "bake at 425" (oven-range) treated as F -> °C
- Leave non-oven small numbers alone; meat-probe duals still become °C-only
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
PROGRESS = ROOT / "scripts" / "tools" / ".standardize_oven_celsius_progress.json"

# Typical oven / bare-F assumption range
OVEN_F_MIN = 200
OVEN_F_MAX = 550


def f_to_c(f: float) -> int:
    c = (f - 32) * 5 / 9
    return int(round(c / 5) * 5)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def fmt_c(c: int) -> str:
    return f"{c}°C"


# Dual: 375F/190C, 375°F / 190°C, (165°F / 74°C), 425°F/220 °C
RE_DUAL = re.compile(
    r"(?i)"
    r"\(?"
    r"(?P<f>\d{2,3})\s*°?\s*F(?:ahrenheit)?"
    r"\s*/\s*"
    r"(?P<c>\d{2,3})\s*°?\s*C(?:elsius|entigrade)?"
    r"\)?"
)

# Explicit F: 350 F, 350°F, 350F, 350 degrees F, 350 F degrees
RE_F = re.compile(
    r"(?i)"
    r"(?P<n>\d{2,3})\s*"
    r"(?:"
    r"°\s*F\b|"
    r"degrees?\s*F(?:ahrenheit)?\b|"
    r"F(?:ahrenheit)?\b(?:\s*degrees?)?"
    r")"
)

# Bare oven number assumed °F when in oven range (200–550).
# Do not match when a unit letter / Celsius / Fahrenheit follows.
RE_BARE_OVEN = re.compile(
    r"(?i)"
    r"(?P<pre>"
    r"(?:pre-?heat(?:\s+the)?\s+oven(?:\s+on)?(?:\s+to)?|"
    r"bake(?:\s+(?:at|in(?:\s+a)?(?:\s+preheated)?(?:\s+oven)?)?)?|"
    r"roast(?:\s+at)?|"
    r"set\s+(?:the\s+)?oven\s+to)"
    r"\s+)"
    r"(?P<n>\d{3})"
    r"(?P<deg>\s*degrees?\b)?"
    r"(?!\s*(?:°\s*)?(?:[FCfc]\b|Celsius\b|Centigrade\b|Fahrenheit\b))"
    r"(?!\s*/\s*\d)"
)

# Explicit C long forms -> normalize to °C
RE_C_LONG = re.compile(
    r"(?i)"
    r"(?P<n>\d{2,3})\s*"
    r"(?:"
    r"degrees?\s*(?:Celsius|Centigrade)\b|"
    r"degrees?\s*C\b|"
    r"°\s*C\b|"
    r"(?:Celsius|Centigrade)\b"
    r")"
)

# Also: "190C" / "190 C" compact without degree symbol (word-boundary safe)
RE_C_COMPACT = re.compile(r"(?i)(?P<n>\d{2,3})\s*C\b(?!\s*(?:elsius|entigrade))")


def convert_text(text: str) -> str | None:
    if not text:
        return None
    original = text

    def repl_c(m: re.Match[str]) -> str:
        return fmt_c(int(m.group("n")))

    # Already-Celsius first (so bare-F logic never sees "200 degrees Celsius")
    text = RE_C_LONG.sub(repl_c, text)
    text = RE_C_COMPACT.sub(repl_c, text)

    def repl_dual(m: re.Match[str]) -> str:
        return fmt_c(f_to_c(int(m.group("f"))))

    text = RE_DUAL.sub(repl_dual, text)

    def repl_f(m: re.Match[str]) -> str:
        return fmt_c(f_to_c(int(m.group("n"))))

    text = RE_F.sub(repl_f, text)

    def repl_bare(m: re.Match[str]) -> str:
        n = int(m.group("n"))
        if OVEN_F_MIN <= n <= OVEN_F_MAX:
            return m.group("pre") + fmt_c(f_to_c(n))
        # e.g. "Preheat oven to 180 degrees" already Celsius-ish
        if m.group("deg") and 100 <= n < OVEN_F_MIN:
            return m.group("pre") + fmt_c(n)
        return m.group(0)

    text = RE_BARE_OVEN.sub(repl_bare, text)

    # Tidy doubled spaces from removals
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Fix missing space before/after °C glued to letters
    text = re.sub(r"([a-zA-Z])(\d{2,3}°C)", r"\1 \2", text)
    text = re.sub(r"(\d{2,3}°C)([a-zA-Z])", r"\1 \2", text)

    if text == original:
        return None
    return text


FIELDS = ("directions", "description", "notes")


def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    changes: list[str] = []
    out = dict(rec)
    for field in FIELDS:
        old = rec.get(field) or ""
        new = convert_text(old)
        if new is not None:
            out[field] = new
            # sample first change
            for a, b in zip(old.splitlines(), new.splitlines()):
                if a != b:
                    changes.append(f"{field}: {a.strip()[:90]} => {b.strip()[:90]}")
                    break
            else:
                changes.append(f"{field}: updated")
    if not changes:
        return None, []
    return out, changes


async def main() -> None:
    demos = [
        "Preheat the oven on to 425 degrees.",
        "Preheat oven to 350 F.",
        "Bake at 375F/190C for 20 minutes.",
        "Preheat oven to 180 degrees Celsius.",
        "until 165°F / 74°C",
        "Preheat the oven to 200 degrees Celsius.",
        "roast at 425",
    ]
    safe_print("Demos:")
    for d in demos:
        safe_print(f"  {d!r}")
        safe_print(f"  -> {convert_text(d)!r}")

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
                for c in changes[:3]:
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
