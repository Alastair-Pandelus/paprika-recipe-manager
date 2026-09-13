"""Find duplicate Field Doctor recipes: manual vs reverse-engineered."""
from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from pathlib import Path

import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def normalize_dish(name: str) -> str:
    n = name or ""
    n = re.sub(r"\s*\(Low FODMAP\)\s*", " ", n, flags=re.I)
    n = re.sub(r"^Field Doctor[- ]Style\s+", "", n, flags=re.I)
    n = re.sub(r"^Field Doctor[:\s-]+", "", n, flags=re.I)
    n = re.sub(r"^FD[:\s-]+", "", n, flags=re.I)
    n = re.sub(r"\bLow[\s-]?FODMAP\b", " ", n, flags=re.I)
    n = n.lower()
    n = n.replace("&", " and ").replace("+", " and ")
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # common synonyms / noise
    replacements = {
        "original porridge": "porridge classic",
        "classic porridge": "porridge classic",
        "cherry and chocolate porridge": "porridge cherry chocolate",
        "no sugar banana porridge": "porridge no sugar banana",
        "cinnamon porridge": "porridge cinnamon",
        "peanut choc chunk bar": "peanut choc chunk",
        "peanut chocolate chunk bar": "peanut choc chunk",
        "double chocolate bar": "double chocolate bar",
        "lemon coconut bar": "lemon coconut bar",
        "banana peanut butter bar": "banana peanut butter bar",
        "hazelnut mocha bar": "hazelnut mocha bar",
    }
    return replacements.get(n, n)


def classify(recipe: dict) -> str:
    name = recipe.get("name") or ""
    source = (recipe.get("source") or "").lower()
    url = (recipe.get("source_url") or "").lower()
    notes = (recipe.get("notes") or "").lower()
    desc = (recipe.get("description") or "").lower()
    cats = recipe.get("categories") or []

    is_style = bool(re.search(r"field doctor[- ]style", name, re.I))
    fd_url = "fielddoctor.co.uk" in url
    inspired = any(
        x in notes or x in desc
        for x in ("inspired recreation", "reverse", "scaled from", "home cooking")
    )
    has_fd_cat = FD in cats

    if is_style or (fd_url and inspired) or (is_style and has_fd_cat):
        return "reverse-engineered"
    if "field doctor" in name.lower() or "fielddoctor" in url or source == "field doctor" or has_fd_cat:
        return "manual-or-other"
    if "field doctor" in notes or "field doctor" in desc or "fielddoctor" in notes:
        return "manual-or-other"
    return "unrelated"


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

        recipes = []
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=H
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if recipe.get("in_trash"):
                continue
            kind = classify(recipe)
            if kind == "unrelated":
                # also catch weak name matches
                blob = " ".join(
                    [
                        recipe.get("name") or "",
                        recipe.get("source") or "",
                        recipe.get("source_url") or "",
                        recipe.get("notes") or "",
                        recipe.get("description") or "",
                    ]
                ).lower()
                if "field doctor" not in blob and "fielddoctor" not in blob:
                    continue
                kind = "manual-or-other"
            recipes.append((kind, recipe))

        print(f"Field Doctor–related recipes (not in trash): {len(recipes)}\n")

        by_key: dict[str, list] = defaultdict(list)
        for kind, recipe in recipes:
            key = normalize_dish(recipe.get("name") or "")
            by_key[key].append((kind, recipe))

        dup_groups = {k: v for k, v in by_key.items() if len(v) > 1}
        singles_re = [
            (k, v[0])
            for k, v in by_key.items()
            if len(v) == 1 and v[0][0] == "reverse-engineered"
        ]
        singles_manual = [
            (k, v[0])
            for k, v in by_key.items()
            if len(v) == 1 and v[0][0] != "reverse-engineered"
        ]

        print("=== DUPLICATE GROUPS (same normalised dish name) ===")
        if not dup_groups:
            print("(none by exact normalised name)\n")
        for key in sorted(dup_groups):
            group = dup_groups[key]
            print(f"\n[{key}]  ({len(group)} copies)")
            for kind, recipe in sorted(group, key=lambda x: x[0]):
                cats = recipe.get("categories") or []
                print(
                    f"  - {kind:20} | {recipe.get('name')}"
                    f"\n      uid={recipe.get('uid')}"
                    f" | servings={recipe.get('servings')}"
                    f" | source={recipe.get('source')!r}"
                    f" | url={recipe.get('source_url') or '-'}"
                    f" | FD_cat={FD in cats} LF_cat={LF in cats}"
                    f" | photo={bool(recipe.get('photo_url'))}"
                    f" | ings={len((recipe.get('ingredients') or '').strip().splitlines())}"
                )

        # Fuzzy: reverse-engineered dish contained in a manual name or vice versa
        print("\n=== POSSIBLE NEAR-MATCHES (manual vs reverse-engineered) ===")
        re_list = [(normalize_dish(r.get("name") or ""), kind, r) for kind, r in recipes if kind == "reverse-engineered"]
        man_list = [(normalize_dish(r.get("name") or ""), kind, r) for kind, r in recipes if kind != "reverse-engineered"]
        near = []
        already = set()
        for mk, mkind, mr in man_list:
            for rk, rkind, rr in re_list:
                if not mk or not rk:
                    continue
                if mk == rk:
                    continue  # already in dup groups
                if mk in rk or rk in mk or (
                    len(mk) > 8 and len(set(mk.split()) & set(rk.split())) >= max(2, min(len(mk.split()), len(rk.split())) - 1)
                ):
                    pair = tuple(sorted([mr["uid"], rr["uid"]]))
                    if pair in already:
                        continue
                    already.add(pair)
                    near.append((mk, mr, rk, rr))
        if not near:
            print("(none beyond exact groups)")
        for mk, mr, rk, rr in sorted(near, key=lambda x: x[0]):
            print(f"\n  manual: {mr.get('name')}  [{mk}]")
            print(f"  reverse: {rr.get('name')}  [{rk}]")

        print("\n=== REVERSE-ENGINEERED ONLY (no exact duplicate) ===")
        for key, (kind, recipe) in sorted(singles_re):
            print(f"  - {recipe.get('name')}")

        print("\n=== MANUAL/OTHER ONLY (no exact reverse twin) ===")
        for key, (kind, recipe) in sorted(singles_manual):
            print(f"  - {recipe.get('name')}  [{key}]  source={recipe.get('source')!r}")


asyncio.run(main())
