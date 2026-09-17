"""
Rename cilantro -> coriander (UK) across Paprika recipes.

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
PROGRESS = ROOT / "scripts" / "tools" / ".cilantro_to_coriander_progress.json"

FIELDS = ("name", "ingredients", "directions", "description", "notes")
PAT = re.compile(r"\bcilantros?\b", re.I)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def match_case(sample: str, replacement: str) -> str:
    if sample.isupper():
        return replacement.upper()
    if sample[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def convert_text(text: str) -> str | None:
    if not text:
        return None
    original = text

    def repl(m: re.Match[str]) -> str:
        raw = m.group(0)
        # cilantro / cilantros -> coriander (UK herb name; seeds stay "coriander seeds")
        return match_case(raw, "coriander")

    text = PAT.sub(repl, text)
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
        "2 tbsp fresh cilantro, chopped",
        "Cilantro Rice",
        "CILANTRO",
        "ground coriander already UK",
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
