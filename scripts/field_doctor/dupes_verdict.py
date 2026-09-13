"""Deeper FD duplicate check: product URL + known aliases."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import aiohttp

_PROJECT = Path(__file__).resolve().parents[2]
ENV = _PROJECT / ".env"
if not ENV.is_file():
    ENV = Path.home() / ".paprika-mcp.env"
BASE = "https://paprikaapp.com/api"

ALIASES = {
    # reverse-engineered product handle/path fragment -> also match these manual name tokens
    "beef-ragu-penne": ["beef ragu", "beef bolognese", "ragu penne"],
    "sweet-potato-spinach-korma": ["sweet potato", "spinach korma", "vegetable korma"],
    "chicken-paella": ["chicken paella", "seafood paella"],  # related but may differ
    "goan": ["goan fish"],
}

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def is_reverse(name: str) -> bool:
    return bool(re.search(r"field doctor[- ]style", name or "", re.I))


def is_manual_fd(name: str, source: str, url: str) -> bool:
    n = (name or "").lower()
    if is_reverse(name):
        return False
    return (
        n.startswith("field doctor")
        or "fielddoctor" in (url or "").lower()
        or "field doctor" in (source or "").lower()
        or "fielddoctor" in (source or "").lower()
    )


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
        ) as r:
            token = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {token}"}
        async with session.get(f"{BASE}/v2/sync/recipes/", headers=H) as r:
            index = (await r.json())["result"]

        all_r = []
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=H
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if recipe.get("in_trash"):
                continue
            all_r.append(recipe)

        manuals = [
            r
            for r in all_r
            if is_manual_fd(r.get("name") or "", r.get("source") or "", r.get("source_url") or "")
        ]
        reverses = [r for r in all_r if is_reverse(r.get("name") or "")]

        print(f"Manual FD-named: {len(manuals)}")
        print(f"Reverse-engineered Style: {len(reverses)}\n")

        # Known product pairs from import history
        pairs = [
            (
                "Beef Ragu / Bolognese",
                "Field Doctor - Beef Ragu Penne",
                "Field Doctor-Style Beef Bolognese + Penne (Low FODMAP)",
                "same product (beef-ragu-penne)",
            ),
            (
                "Sweet Potato Spinach Korma / Vegetable Korma",
                "Field Doctor - Sweet Potato + Spinach Korma",
                "Field Doctor-Style Vegetable Korma (Low FODMAP)",
                "same product (sweet-potato-spinach-korma)",
            ),
            (
                "Seafood Paella vs Chicken Paella",
                "Field Doctor - Seafood Paella",
                "Field Doctor-Style Chicken Paella (Low FODMAP)",
                "related name only — different dishes (seafood vs chicken)",
            ),
            (
                "Goan Fish Curry",
                "Field Doctor - Goan Fish Curry",
                None,
                "manual only — no reverse-engineered Low FODMAP twin imported",
            ),
        ]

        by_name = {r.get("name"): r for r in all_r}

        print("=== VERDICT ===\n")
        for label, man_name, rev_name, note in pairs:
            man = by_name.get(man_name)
            rev = by_name.get(rev_name) if rev_name else None
            print(f"{label}")
            print(f"  note: {note}")
            if man:
                print(
                    f"  MANUAL : {man['name']}"
                    f" | servings={man.get('servings')}"
                    f" | ings={len((man.get('ingredients') or '').splitlines())}"
                    f" | uid={man['uid']}"
                )
            else:
                print(f"  MANUAL : missing ({man_name})")
            if rev_name:
                if rev:
                    print(
                        f"  REVERSE: {rev['name']}"
                        f" | servings={rev.get('servings')}"
                        f" | ings={len((rev.get('ingredients') or '').splitlines())}"
                        f" | uid={rev['uid']}"
                        f" | url={rev.get('source_url')}"
                    )
                else:
                    print(f"  REVERSE: missing ({rev_name})")
            print()

        # Any other manuals?
        known_manual = {
            "Field Doctor - Beef Ragu Penne",
            "Field Doctor - Sweet Potato + Spinach Korma",
            "Field Doctor - Seafood Paella",
            "Field Doctor - Goan Fish Curry",
        }
        extras = [m for m in manuals if m.get("name") not in known_manual]
        print("=== OTHER MANUAL FD RECIPES ===")
        if not extras:
            print("(none)")
        for m in extras:
            print(f"  - {m.get('name')} | source={m.get('source')!r} | url={m.get('source_url')}")


asyncio.run(main())
