"""Scan Field Doctor Paprika recipes for duplicate ingredients."""
from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from pathlib import Path

import aiohttp

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def normalize_key(line: str) -> str | None:
    """Extract a comparable ingredient key from an ingredient line."""
    line = line.strip()
    if not line:
        return None
    # Skip header / note lines
    if line.startswith("Scaled for ") or line.startswith("Unlabelled "):
        return None
    if line.startswith("Spoon measures ") or line.startswith("Amounts above "):
        return None
    if "counted as" in line or line.startswith("Lemon/lime"):
        return None
    if line.startswith("Chopped tomatoes counted"):
        return None

    # Drop trailing [...] notes
    core = re.sub(r"\s*\[[^\]]*\]\s*$", "", line).strip()
    while re.search(r"\[[^\]]*\]\s*$", core):
        core = re.sub(r"\s*\[[^\]]*\]\s*$", "", core).strip()

    # Strip leading quantity patterns
    # e.g. "670 g ...", "1 1/2 tbsp ...", "3 medium ...", "1 1/2 x 400 g tins ...", "1/2 Lemon Juice"
    qty = (
        r"^(?:"
        r"pinch of\s+|"
        r"\d+(?:\s+\d+/\d+)?(?:\.\d+)?\s*x\s+\d+\s*g\s+tins?\s+|"
        r"\d+(?:\s+\d+/\d+)?(?:\.\d+)?\s*(?:g|kg|ml|tsp|tbsp|x)\s+|"
        r"\d+/\d+\s+|"
        r"\d+(?:\s+\d+/\d+)?\s+"
        r")"
    )
    name = re.sub(qty, "", core, count=1, flags=re.I).strip()
    # Drop leading "medium "
    name = re.sub(r"(?i)^medium\s+", "", name).strip()
    # Primary name before ( or [
    name = re.split(r"[\[\({]", name, maxsplit=1)[0].strip()
    name = re.sub(r"\s+", " ", name).strip(" ,.")
    if not name:
        return None
    # Singular-ish normalize
    key = name.lower()
    if key.endswith("oes"):
        pass
    elif key.endswith("ies"):
        key = key[:-3] + "y"
    elif key.endswith("s") and not key.endswith("ss"):
        key = key[:-1]
    return key


async def main() -> None:
    load_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        findings = []
        clean = 0
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            name = recipe.get("name") or "?"
            ings = (recipe.get("ingredients") or "").splitlines()
            by_key: dict[str, list[str]] = defaultdict(list)
            for line in ings:
                key = normalize_key(line)
                if key:
                    by_key[key].append(line.strip())

            dups = {k: v for k, v in by_key.items() if len(v) > 1}
            if dups:
                findings.append((name, dups))
            else:
                clean += 1

        print(f"Field Doctor recipes scanned: {clean + len(findings)}")
        print(f"With duplicate ingredient keys: {len(findings)}")
        print(f"Clean: {clean}")
        print()
        for name, dups in findings:
            print(f"## {name}")
            for key, lines in sorted(dups.items()):
                print(f"  duplicate key: {key!r} ({len(lines)}x)")
                for ln in lines:
                    print(f"    - {ln}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
