"""Clean remaining dual-option LF ingredient lines to a single LF choice."""
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


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


LINE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "High Fiber Steel Cut Overnight Oats",
        re.compile(r"(?i)^\s*1\s*[½1/2]+\s*cups?\s+non-dairy milk"),
        "1 ½ cups unsweetened almond milk",
    ),
    (
        "Easy 20 Minute Tofu Burgers",
        re.compile(r"(?i)^\s*½\s*tsp ground cumin"),
        "1 tsp ground cumin",
    ),
    (
        "Fresh + Filling Cold Tofu with Tomato Cucumber Salad",
        re.compile(r"(?i)^\s*1 small roma tomato"),
        "1 small roma tomato",
    ),
    (
        "20 Min Low FODMAP Vegan Pasta Salad",
        re.compile(r"(?i)^\s*350 g of your favourite pasta"),
        "350 g gluten-free pasta",
    ),
    (
        "Crispy Baked Tofu Paneer with Mint Chutney",
        re.compile(r"(?i)^\s*1/2 cup unsweetened almond milk or macadamia milk"),
        "1/2 cup unsweetened almond milk",
    ),
    (
        "Creamy Snickers Tofu Smoothie (37g protein!)",
        re.compile(r"(?i)^\s*1 cup unsweetened almond milk or macadamia milk"),
        "1 cup unsweetened almond milk",
    ),
    (
        "Karnı yarık (Stuffed eggplant with minced meat)",
        re.compile(r"(?i)^\s*A few large tbsp Turkish yoghurt"),
        "A few large tbsp lactose-free yoghurt",
    ),
    (
        "Fody's Cheesy Gordita Crunch Copycat",
        re.compile(r"(?i)^\s*4-6 small soft corn tortillas"),
        "4-6 small soft corn tortillas (plain corn, no onion/garlic)",
    ),
    (
        "Low FODMAP barbeque tempeh sandwich",
        re.compile(r"(?i)^\s*4 tbsp BBQ sauce"),
        "4 tbsp low FODMAP BBQ sauce",
    ),
    (
        "Sesame Lime Dressing with Ginger (5 minutes!)",
        re.compile(r"(?i)^\s*1–2 tbsp garlic-infused olive oil"),
        "1–2 tbsp garlic-infused olive oil",
    ),
]

DESC_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "20 Min Low FODMAP Vegan Pasta Salad",
        re.compile(r"(?i)Low FODMAP or regular option"),
        "Low FODMAP",
    ),
    (
        "Sesame Lime Dressing with Ginger (5 minutes!)",
        re.compile(r"(?i), with a low FODMAP option for sensitive tummies!"),
        ". Low FODMAP.",
    ),
]


async def main() -> None:
    want = {n for n, _, _ in LINE_RULES} | {n for n, _, _ in DESC_RULES}
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.4)
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
        saved = 0
        for e in body["result"]:
            st, body2 = await api_json(
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{e['uid']}/",
                headers=headers,
            )
            rec = (body2 or {}).get("result") or {}
            name = rec.get("name") or ""
            if name not in want:
                continue

            changed = False
            ings = rec.get("ingredients") or ""
            new_lines = []
            for line in ings.splitlines():
                nl = line
                for n, pat, repl in LINE_RULES:
                    if n == name and pat.match(line):
                        nl = repl
                        break
                if nl != line:
                    safe_print(f"{name}: {line!r} => {nl!r}")
                    changed = True
                new_lines.append(nl)
            if changed:
                joined = "\n".join(new_lines)
                if ings.endswith("\n"):
                    joined += "\n"
                rec["ingredients"] = joined

            desc = rec.get("description") or ""
            for n, pat, repl in DESC_RULES:
                if n != name:
                    continue
                nd = pat.sub(repl, desc)
                if nd != desc:
                    safe_print(f"{name}: DESC updated")
                    rec["description"] = nd
                    changed = True

            # Sesame dressing: ensure oil is used in directions
            if name.startswith("Sesame Lime Dressing"):
                dirs = rec.get("directions") or ""
                if "garlic-infused" not in dirs.lower():
                    rec["directions"] = (
                        "1. Place lime juice, toasted sesame oil, garlic-infused olive oil, "
                        "soy sauce, maple syrup, ginger, and Sriracha (if using) into a jam jar. "
                        "Fasten the lid and shake.\n\n"
                        "2. Store up to 7 days in the fridge.\n"
                    )
                    changed = True
                    safe_print(f"{name}: DIRS updated")

            if not changed:
                safe_print(f"skip (no change): {name}")
                continue

            rec["hash"] = calc_hash(rec)
            ok = await save_recipe(s, limiter, headers, rec)
            safe_print(f"SAVE {name} {ok}")
            if ok:
                saved += 1

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        safe_print(f"Done saved={saved}")


if __name__ == "__main__":
    asyncio.run(main())
