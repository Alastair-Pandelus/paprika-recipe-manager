"""
Replace US vegetable names with UK terms across Paprika recipes:
  zucchini / zucchinis  -> courgette / courgettes
  eggplant / eggplants / egg plant -> aubergine / aubergines

Applies to name, ingredients, directions, description, notes.
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
PROGRESS = ROOT / "scripts" / "tools" / ".uk_veg_terms_progress.json"

FIELDS = ("name", "ingredients", "directions", "description", "notes")

# (pattern, singular UK, plural UK) — matched case-insensitively; case preserved from match
REPLACEMENTS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bzucchinis\b", re.I), "courgette", "courgettes"),
    (re.compile(r"\bzucchini\b", re.I), "courgette", "courgettes"),
    (re.compile(r"\begg\s*plants\b", re.I), "aubergine", "aubergines"),
    (re.compile(r"\begg\s*plant\b", re.I), "aubergine", "aubergines"),
]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def match_case(sample: str, replacement: str) -> str:
    """Apply casing of sample onto replacement (title / upper / lower)."""
    if sample.isupper():
        return replacement.upper()
    if sample[:1].isupper() and sample[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    if sample[:1].isupper():
        # Title-ish / mixed: capitalize first letter only
        return replacement[:1].upper() + replacement[1:]
    return replacement


def convert_text(text: str) -> str | None:
    if not text:
        return None
    original = text

    def make_repl(singular: str, plural: str):
        def _repl(m: re.Match[str]) -> str:
            raw = m.group(0)
            # choose plural form if matched token looks plural
            is_plural = raw.lower().endswith("s") and not raw.lower().endswith("us")
            # zucchini ends with i; zucchinis with s; eggplant(s)
            if re.search(r"(?i)zucchinis$|plants$|plant\s*s$", raw.replace(" ", "")):
                is_plural = True
            elif re.search(r"(?i)^zucchinis$|^egg\s*plants$", raw):
                is_plural = True
            else:
                is_plural = bool(re.search(r"(?i)s$", raw)) and not re.search(
                    r"(?i)zucchini$", raw
                )
            uk = plural if is_plural else singular
            return match_case(raw, uk)

        return _repl

    # Process plurals first (already ordered that way)
    for pat, singular, plural in REPLACEMENTS:
        # Detect plural from which pattern fired
        def repl(m: re.Match[str], s=singular, p=plural, pattern=pat) -> str:
            raw = m.group(0)
            # plural patterns are the ones whose pattern source contains 's\b' after the noun
            src = pattern.pattern.lower()
            use_plural = "zucchinis" in src or "plants" in src
            uk = p if use_plural else s
            return match_case(raw, uk)

        text = pat.sub(repl, text)

    if text == original:
        return None
    return text


def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    changes: list[str] = []
    out = dict(rec)
    for field in FIELDS:
        old = rec.get(field) or ""
        new = convert_text(old)
        if new is None:
            continue
        out[field] = new
        # sample one diff line / snippet
        if field == "name":
            changes.append(f"name: {old} => {new}")
        else:
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
        "1 medium zucchini, sliced",
        "2 Zucchinis",
        "1 eggplant",
        "Eggplants, diced",
        "egg plant puree",
        "Grilled Eggplant Salad",
        "courgette already UK",
    ]
    safe_print("Demos:")
    for d in demos:
        safe_print(f"  {d!r} => {convert_text(d)!r}")

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
