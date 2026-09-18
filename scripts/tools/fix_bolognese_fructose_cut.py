"""Apply fructose cut to Beef Bolognese and re-score FODMAP."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fodmap_score_lib import (  # noqa: E402
    strip_inline_fodmap,
    transform_recipe,
)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

UID = "E47DB200-625A-488F-AA9B-1E4A7EBB3178"
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def cut_fructose_lines(ingredients: str) -> str:
    """Halve tomato/pepper/olive fructose drivers; strip old FODMAP tags first."""
    out = []
    for line in (ingredients or "").splitlines():
        if not line.strip():
            continue
        clean = strip_inline_fodmap(line)
        low = clean.lower()

        # 1/4 x 400 g tin → 1/8 x 400 g (~50 g)
        if "plum tomato" in low or "chopped tomato" in low:
            clean = re.sub(
                r"^1/4\s*x\s*400\s*g\s*tins?",
                "1/8 x 400 g tins",
                clean,
                flags=re.I,
            )
            clean = re.sub(
                r"^¼\s*x\s*400\s*g\s*tins?",
                "1/8 x 400 g tins",
                clean,
                flags=re.I,
            )
            out.append(clean)
            continue

        # 1/4 Red Peppers → 1/8 (or keep green preference later)
        if re.search(r"\bred peppers?\b", low) and "black" not in low:
            clean = re.sub(r"^1/4\b", "1/8", clean)
            clean = re.sub(r"^¼\b", "1/8", clean)
            out.append(clean)
            continue

        # 2 tsp puree → 1 tsp
        if "tomato puree" in low or "tomato paste" in low:
            clean = re.sub(r"^2\s*tsp\b", "1 tsp", clean, flags=re.I)
            clean = re.sub(r"^2\s*teaspoons?\b", "1 tsp", clean, flags=re.I)
            out.append(clean)
            continue

        # sundried ~2.8 g → ~1.4 g → 1.5 g
        if "sundried" in low or "sun-dried" in low or "sun dried" in low:
            clean = re.sub(r"^[\d.]+(\s*g)\b", "1.5\\1", clean, count=1)
            out.append(clean)
            continue

        # olives 3.8 g → 2 g
        if "olive" in low and "oil" not in low:
            clean = re.sub(r"^[\d.]+(\s*g)\b", "2\\1", clean, count=1)
            out.append(clean)
            continue

        out.append(clean)
    return "\n".join(out).rstrip() + "\n"


async def main() -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    local = con.execute(
        "SELECT name, ingredients, description, notes FROM recipes WHERE uid=?",
        (UID,),
    ).fetchone()
    if not local:
        raise SystemExit("missing recipe")

    cut = cut_fructose_lines(local["ingredients"] or "")
    # clear stale desc fodmap before transform
    t = transform_recipe(
        # strip title emoji before transform so meal emoji is recomputed
        re.sub(r"^[🟢🟡🟠🔴]\s*", "", local["name"] or ""),
        cut,
        local["description"] or "",
        local["notes"] or "",
    )

    safe_print("=== NEW TITLE ===")
    safe_print(t["name"])
    safe_print("=== FRUCTOSE / TRACKED LINES ===")
    for line in t["ingredients"].splitlines():
        if any(x in line for x in ("Fructose", "Fructans", "tomato", "Pepper", "Olive", "Penne", "Sundried")):
            safe_print(line)
    safe_print("=== DESC TAIL ===")
    safe_print((t["description"] or "")[-300:])
    safe_print("stacks", t["stacks"], "meal", t["meal_emoji"])

    if t["meal_emoji"] == "🔴":
        safe_print("WARNING: still red — aborting save")
        con.close()
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}
        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=H
        )
        rec = (body or {}).get("result") or {}
        if not rec.get("uid"):
            raise SystemExit("cloud missing")

        cut2 = cut_fructose_lines(rec.get("ingredients") or "")
        t2 = transform_recipe(
            re.sub(r"^[🟢🟡🟠🔴]\s*", "", rec.get("name") or ""),
            cut2,
            rec.get("description") or "",
            rec.get("notes") or "",
        )
        if t2["meal_emoji"] == "🔴":
            raise SystemExit("still red after cut")

        rec["name"] = t2["name"]
        rec["ingredients"] = t2["ingredients"]
        rec["description"] = t2["description"]
        rec["notes"] = t2["notes"]
        rec["hash"] = calc_hash(rec)

        await limiter.wait_turn()
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(rec),
            content_type="application/octet-stream",
            filename="data",
        )
        async with s.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=H, data=form
        ) as r:
            ok = '"result":true' in (await r.text()).replace(" ", "")
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            await r.text()

    if not ok:
        raise SystemExit("save failed")
    con.execute(
        "UPDATE recipes SET name=?, ingredients=?, description=?, notes=?, status=? WHERE uid=?",
        (
            t2["name"],
            t2["ingredients"],
            t2["description"],
            t2["notes"],
            "modified",
            UID,
        ),
    )
    con.commit()
    con.close()
    safe_print(f"Applied. Meal now {t2['meal_emoji']}")


if __name__ == "__main__":
    asyncio.run(main())
