"""Remediate a 🔴 Paprika recipe to ≤🟠 by scaling down over-limit FODMAP drivers.

  python scripts/tools/remediate_red_fodmap_recipe.py --uid <UID>
  python scripts/tools/remediate_red_fodmap_recipe.py --name "Cottage Pie"
  python scripts/tools/remediate_red_fodmap_recipe.py --uid <UID> --apply
  python scripts/tools/remediate_red_fodmap_recipe.py --uid <UID> --target 1.0 --apply

Default target stack per type = 1.4 (meal 🟠 max). Use --target 1.0 for 🟡.
"""
from __future__ import annotations

import argparse
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
    TYPE_NAME,
    TYPE_ORDER,
    format_tag,
    meal_emoji,
    meal_score_emoji,
    parse_line,
    pct,
    score_ingredient_line,
    strip_inline_fodmap,
    to_grams,
    transform_recipe,
)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

FRACTION_CANDIDATES = [
    0.0625,
    0.125,
    0.25,
    0.333,
    0.5,
    0.666,
    0.75,
    1.0,
    1.25,
    1.5,
    2.0,
    2.5,
    3.0,
    4.0,
]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def _fraction_label(best: float) -> str:
    labels = {
        0.0625: "1/16",
        0.125: "1/8",
        0.25: "1/4",
        0.333: "1/3",
        0.5: "1/2",
        0.666: "2/3",
        0.75: "3/4",
        1.0: "1",
        1.25: "1 1/4",
        1.5: "1 1/2",
        2.0: "2",
        2.5: "2 1/2",
        3.0: "3",
        4.0: "4",
    }
    return labels.get(best, f"{best:g}")


def _snap_fraction(value: float, *, floor: bool) -> float:
    if floor:
        le = [c for c in FRACTION_CANDIDATES if c <= value + 1e-9]
        return max(le) if le else min(FRACTION_CANDIDATES)
    return min(FRACTION_CANDIDATES, key=lambda c: abs(c - value))


def format_kitchen_qty(value: float, unit: str | None, *, floor: bool = False) -> str:
    # Convert tiny cups/tbsp down so remediation can actually cut load
    if unit == "cup" and value < 0.125:
        return format_kitchen_qty(value * 16.0, "tbsp", floor=floor)
    if unit == "tbsp" and value < 0.25:
        return format_kitchen_qty(value * 3.0, "tsp", floor=floor)

    if unit in {"g", "ml", "oz"}:
        if value >= 10:
            return f"{int(round(value))} {unit}"
        if value >= 1:
            return f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        if value < 0.05:
            value = 0.05
        return f"{value:.2f}".rstrip("0").rstrip(".") + f" {unit}"
    if unit in {"tsp", "tbsp", "cup", "count", None} or unit in {
        "medium",
        "large",
        "small",
    }:
        best = _snap_fraction(value, floor=floor)
        q = _fraction_label(best)
        if unit in {None, "count"}:
            return q
        if unit in {"medium", "large", "small"}:
            return f"{q} {unit}"
        return f"{q} {unit}"
    return f"{value:g} {unit}" if unit else f"{value:g}"


def rewrite_qty_prefix(
    line: str, new_value: float, unit: str | None, rest: str, *, floor: bool = True
) -> str:
    """Rebuild ingredient line with new quantity, preserving rest/name."""
    # Special: tin pattern
    if re.search(r"\btins?\b", line, re.I) and unit == "g":
        # express as fraction of 400 g tin — prefer floor when remediating
        tin_frac = new_value / 400.0
        tin_opts = [0.125, 0.25, 0.333, 0.5, 0.75, 1.0]
        if floor:
            le = [c for c in tin_opts if c <= tin_frac + 1e-9]
            best = max(le) if le else tin_opts[0]
        else:
            best = min(tin_opts, key=lambda c: abs(c - tin_frac))
        labels = {
            0.125: "1/8",
            0.25: "1/4",
            0.333: "1/3",
            0.5: "1/2",
            0.75: "3/4",
            1.0: "1",
        }
        return f"{labels[best]} x 400 g tins {rest}".strip()

    qty = format_kitchen_qty(new_value, unit, floor=floor)
    # count of peppers/carrots: "1/8 Red Peppers"
    if unit == "count":
        return f"{qty} {rest}".strip()
    return f"{qty} {rest}".strip()


def diagnose(ingredients: str):
    rows = []
    stacks: dict[str, float] = {}
    for line in (ingredients or "").splitlines():
        if not line.strip():
            continue
        scored = score_ingredient_line(line)
        if not scored:
            continue
        clean, grams, green, ftype, no_lim = scored
        if no_lim or not ftype or not green:
            continue
        ratio = grams / green
        stacks[ftype] = stacks.get(ftype, 0.0) + ratio
        p = parse_line(clean)
        rows.append(
            {
                "clean": clean,
                "grams": grams,
                "green": green,
                "ftype": ftype,
                "ratio": ratio,
                "parsed": p,
            }
        )
    return rows, stacks


def _scale_pass(ingredients: str, target: float) -> tuple[str, dict]:
    """One scale-down pass for over-target types (floor snap)."""
    rows, stacks = diagnose(ingredients)
    factors = {
        t: (target / load if load > target else 1.0) for t, load in stacks.items()
    }

    out = []
    changes = []
    for line in (ingredients or "").splitlines():
        if not line.strip():
            continue
        clean = strip_inline_fodmap(line)
        scored = score_ingredient_line(clean)
        if not scored:
            out.append(clean)
            continue
        _c, grams, green, ftype, no_lim = scored
        if no_lim or not ftype or not green:
            out.append(clean)
            continue
        factor = factors.get(ftype, 1.0)
        if factor >= 0.999:
            out.append(clean)
            continue
        p = parse_line(clean)
        if not p:
            out.append(clean)
            continue
        new_val = p.value * factor
        if p.unit == "g" and re.search(r"\btins?\b", clean, re.I):
            new_line = rewrite_qty_prefix(clean, grams * factor, "g", p.rest, floor=True)
        elif p.unit == "count":
            new_line = rewrite_qty_prefix(clean, new_val, "count", p.rest, floor=True)
        else:
            new_line = rewrite_qty_prefix(clean, new_val, p.unit, p.rest, floor=True)
        if new_line != clean:
            changes.append(
                {
                    "was": clean,
                    "now": new_line,
                    "type": TYPE_NAME[ftype],
                    "factor": round(factor, 3),
                }
            )
        out.append(new_line)

    text = "\n".join(out).rstrip() + "\n"
    return text, {"factors": factors, "changes": changes, "before_stacks": stacks}


def remediate_ingredients(ingredients: str, target: float = 1.4) -> tuple[str, dict]:
    """Scale down lines that feed over-target FODMAP types (iterate until ≤ target)."""
    text = ingredients or ""
    all_changes: list[dict] = []
    first_stacks: dict[str, float] | None = None
    last_factors: dict = {}
    for _ in range(6):
        _, stacks = diagnose(text)
        if first_stacks is None:
            first_stacks = dict(stacks)
        if max(stacks.values(), default=0) <= target:
            break
        new_text, meta = _scale_pass(text, target)
        last_factors = meta["factors"]
        all_changes.extend(meta["changes"])
        if new_text == text or not meta["changes"]:
            break
        text = new_text
    return text, {
        "factors": last_factors,
        "changes": all_changes,
        "before_stacks": first_stacks or {},
    }


def find_recipe(uid: str | None, name: str | None) -> dict:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    if uid:
        r = con.execute(
            "SELECT uid, name, ingredients, description, notes FROM recipes WHERE uid=?",
            (uid,),
        ).fetchone()
    else:
        r = con.execute(
            """
            SELECT uid, name, ingredients, description, notes FROM recipes
            WHERE coalesce(in_trash,0)=0 AND name LIKE ?
            ORDER BY CASE WHEN name LIKE '🔴%' THEN 0 ELSE 1 END, name
            LIMIT 1
            """,
            (f"%{name}%",),
        ).fetchone()
    con.close()
    if not r:
        raise SystemExit("Recipe not found")
    return dict(r)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uid")
    ap.add_argument("--name")
    ap.add_argument("--target", type=float, default=1.4, help="Max stack ratio per type")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not args.uid and not args.name:
        raise SystemExit("Pass --uid or --name")

    rec = find_recipe(args.uid, args.name)
    safe_print(f"Recipe: {rec['name']}")
    safe_print(f"UID: {rec['uid']}")

    rows, stacks = diagnose(rec["ingredients"] or "")
    safe_print("Before stacks:")
    for t in TYPE_ORDER:
        if stacks.get(t, 0) >= 0.05:
            safe_print(f"  {meal_emoji(stacks[t])} {TYPE_NAME[t]} {pct(stacks[t])}%")
    safe_print(f"Meal before: {meal_score_emoji(stacks)}")

    if meal_score_emoji(stacks) != "❌" and max(stacks.values(), default=0) <= args.target:
        safe_print("Already at/under target — nothing to do.")
        return

    new_ings, meta = remediate_ingredients(rec["ingredients"] or "", target=args.target)
    safe_print(f"Scale factors: { {TYPE_NAME.get(k,k): round(v,2) for k,v in meta['factors'].items() if v < 0.999} }")
    for ch in meta["changes"][:12]:
        safe_print(f"  {ch['type']} ×{ch['factor']}: {ch['was'][:50]} → {ch['now'][:50]}")

    base_name = re.sub(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*", "", rec["name"] or "")
    t = transform_recipe(base_name, new_ings, rec["description"] or "", rec["notes"] or "")
    safe_print(f"Meal after: {t['meal_emoji']}  stacks={t['stacks']}")
    safe_print(f"Title: {t['name']}")

    if t["meal_emoji"] == "❌":
        safe_print("Still red after remediation — not saving.")
        return

    if not args.apply:
        safe_print("Dry-run only. Pass --apply to save.")
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
    uid = rec["uid"]
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
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
        )
        cloud = (body or {}).get("result") or {}
        if not cloud.get("uid"):
            raise SystemExit("cloud missing")

        cut, _ = remediate_ingredients(cloud.get("ingredients") or "", target=args.target)
        t2 = transform_recipe(
            re.sub(r"^(?:✅|ℹ️|⚠️|❌|🟢|🟡|🟠|🔴)\s*", "", cloud.get("name") or ""),
            cut,
            cloud.get("description") or "",
            cloud.get("notes") or "",
        )
        if t2["meal_emoji"] == "❌":
            raise SystemExit("still red")

        cloud["name"] = t2["name"]
        cloud["ingredients"] = t2["ingredients"]
        cloud["description"] = t2["description"]
        cloud["notes"] = t2["notes"]
        cloud["hash"] = calc_hash(cloud)

        await limiter.wait_turn()
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(cloud),
            content_type="application/octet-stream",
            filename="data",
        )
        async with s.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H, data=form
        ) as r:
            ok = '"result":true' in (await r.text()).replace(" ", "")
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            await r.text()

    if not ok:
        raise SystemExit("save failed")
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "UPDATE recipes SET name=?, ingredients=?, description=?, notes=?, status=? WHERE uid=?",
        (t2["name"], t2["ingredients"], t2["description"], t2["notes"], "modified", uid),
    )
    con.commit()
    con.close()
    safe_print(f"Applied. Meal now {t2['meal_emoji']}")


if __name__ == "__main__":
    asyncio.run(main())
