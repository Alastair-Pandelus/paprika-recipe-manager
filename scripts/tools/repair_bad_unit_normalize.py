"""
Repair recipes damaged by the first normalize_ingredient_units run,
by re-pulling ingredients from source_url JSON-LD when available,
then applying the fixed normalizer.
"""
from __future__ import annotations

import asyncio
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
    calc_hash,
    gzip_obj,
    normalize_ingredients,
    save_recipe,
)

PROGRESS_BAD = ROOT / "scripts" / "tools" / ".normalize_ingredient_units_progress.json"


def extract_ingredients_from_html(html: str) -> list[str]:
    """Pull recipeIngredient list from JSON-LD if present."""
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    )
    for raw in blocks:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        # @graph
        expanded = []
        for n in nodes:
            if isinstance(n, dict) and "@graph" in n:
                expanded.extend(n["@graph"])
            else:
                expanded.append(n)
        for n in expanded:
            if not isinstance(n, dict):
                continue
            t = n.get("@type")
            types = t if isinstance(t, list) else [t]
            types = [str(x).lower() for x in types if x]
            if not any("recipe" == x or x.endswith("/recipe") for x in types):
                if "recipeingredient" not in {k.lower() for k in n}:
                    continue
            ings = n.get("recipeIngredient") or n.get("ingredients")
            if isinstance(ings, list) and ings:
                return [str(x).strip() for x in ings if str(x).strip()]
    return []


def safe_print(*a, **k) -> None:
    try:
        print(*a, **k, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), **k, flush=True)


async def main() -> None:
    if not PROGRESS_BAD.exists():
        safe_print("No bad-run progress file; nothing to repair.")
        return
    uids = json.loads(PROGRESS_BAD.read_text(encoding="utf-8"))
    safe_print(f"Repairing up to {len(uids)} recipes from bad run…")

    user, password = paprika_credentials()
    limiter = RateLimiter(min_interval=0.75)
    headers_web = {
        "User-Agent": "Mozilla/5.0 (compatible; PaprikaUnitFix/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        status, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        )
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        repaired = failed = skipped = 0
        for i, uid in enumerate(uids, 1):
            status, body = await api_json(
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            rec = (body or {}).get("result") if isinstance(body, dict) else None
            if not rec:
                failed += 1
                continue
            name = rec.get("name") or ""
            url = (rec.get("source_url") or "").strip()
            if not url:
                skipped += 1
                safe_print(f"SKIP no source_url: {name}")
                continue

            try:
                await limiter.wait_turn()
                async with s.get(url, headers=headers_web) as resp:
                    if resp.status != 200:
                        skipped += 1
                        safe_print(f"SKIP fetch {resp.status}: {name}")
                        continue
                    html = await resp.text()
            except Exception as ex:
                skipped += 1
                safe_print(f"SKIP err {ex}: {name}")
                continue

            ings = extract_ingredients_from_html(html)
            if not ings:
                skipped += 1
                safe_print(f"SKIP no JSON-LD ings: {name}")
                continue

            text = "\n".join(ings) + "\n"
            normalized = normalize_ingredients(text) or text
            rec["ingredients"] = normalized
            rec["hash"] = calc_hash(rec)
            if await save_recipe(s, limiter, headers, rec):
                repaired += 1
                safe_print(f"REPAIRED [{repaired}] {name}")
            else:
                failed += 1
                safe_print(f"FAIL {name}")

            if i % 10 == 0:
                safe_print(f"… {i}/{len(uids)}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        PROGRESS_BAD.unlink(missing_ok=True)
        safe_print(
            f"\nDone repair. repaired={repaired} skipped={skipped} failed={failed}"
        )


if __name__ == "__main__":
    asyncio.run(main())
