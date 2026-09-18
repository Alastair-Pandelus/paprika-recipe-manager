"""Fix mangled spring-onion / leek duplicate ingredient lines.

Broths (Vegetable / Bone / Chicken): keep `5 to 6` (or `4 to 5`) spring onion
green tops with qty first; restore a real leek-greens line; drop corrupted
`1 spring onion green tops, large leek…` duplicates.

Near-duplicate spring-onion pairs elsewhere: keep the more specific line
(usually the one with `(about …)`).

  python scripts/tools/fix_spring_onion_leek_dupes.py
  python scripts/tools/fix_spring_onion_leek_dupes.py --apply
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

from fodmap_score_lib import transform_recipe  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

TAG_RE = re.compile(
    r"(\s+(?:🟢|🟡|🟠|🔴)\s+(?:Fructose|Lactose|Fructans|Galacto-oligosaccharides|Sorbitol|Mannitol)\s+\d+%\s*)$"
)

# Corrupted: "1 spring onion green tops, large leek…"
MANGLED_LEEK = re.compile(
    r"^1 spring onion green tops,\s*large leek\b",
    re.I,
)

# Qty-first spring onion green tops (the good line)
SPRING_TOPS = re.compile(
    r"^(?P<qty>\d+(?:\s+to\s+\d+)?)\s+spring onion green tops\b",
    re.I,
)

# Near-identical spring onion lines differing only by (about …)
ABOUT_PAREN = re.compile(r"\s*\(about[^)]*\)\s*", re.I)
# Extra green-part glosses that don't change the ingredient
GREEN_GLOSS = re.compile(
    r"\s*\((?:green parts? only[^)]*|green tops? only[^)]*|no white bulb[^)]*)\)\s*",
    re.I,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def bare(line: str) -> str:
    return TAG_RE.sub("", line).rstrip()


def core_key(line: str) -> str:
    s = ABOUT_PAREN.sub(" ", bare(line))
    s = GREEN_GLOSS.sub(" ", s)
    s = re.sub(r",\s*green tops only\b", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def fix_ingredients(text: str) -> tuple[str | None, list[dict]]:
    if not text:
        return None, []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    changes: list[dict] = []

    has_mangled = any(MANGLED_LEEK.match(bare(ln)) for ln in lines if ln.strip())
    has_spring_range = any(
        SPRING_TOPS.match(bare(ln)) and "to" in bare(ln).lower()
        for ln in lines
        if ln.strip()
    )

    out: list[str] = []
    inserted_leek = False
    seen_cores: set[str] = set()

    for ln in lines:
        if not ln.strip():
            out.append(ln)
            continue
        body = bare(ln)

        # Drop mangled leek/spring onion hybrids; insert one real leek line once
        if MANGLED_LEEK.match(body):
            changes.append({"was": ln, "now": "(removed mangled leek/spring onion hybrid)"})
            about = ""
            m_about = re.search(r"\(about[^)]*\)", body, re.I)
            if m_about:
                about = " " + m_about.group(0)
            prep = "rinsed and roughly chopped"
            if "thinly sliced" in body.lower():
                prep = "thinly sliced, white base discarded"
            elif "rinsed" in body.lower():
                prep = "rinsed and roughly chopped"
            leek = f"1 large leek, green tops only, {prep}{about}"
            if not inserted_leek:
                out.append(leek)
                changes.append({"was": "(missing)", "now": leek})
                inserted_leek = True
            elif about:
                # Upgrade previously inserted leek with (about …) detail
                for i, prev in enumerate(out):
                    if prev.startswith("1 large leek, green tops only"):
                        if "(about" not in prev.lower():
                            changes.append({"was": prev, "now": leek})
                            out[i] = leek
                        break
            continue

        # Near-duplicate spring onion lines: keep the more specific (about …) variant
        if SPRING_TOPS.match(body) or (
            "spring onion green tops" in body.lower() and not MANGLED_LEEK.match(body)
        ):
            key = core_key(ln)
            if key in seen_cores:
                # Prefer replacing a shorter twin already kept
                replaced = False
                for i, prev in enumerate(out):
                    if not prev.strip():
                        continue
                    if core_key(prev) != key:
                        continue
                    more_specific = (
                        (ABOUT_PAREN.search(body) and not ABOUT_PAREN.search(bare(prev)))
                        or (GREEN_GLOSS.search(body) and not GREEN_GLOSS.search(bare(prev)))
                        or (len(body) > len(bare(prev)) + 8)
                    )
                    if more_specific:
                        changes.append({"was": prev, "now": body})
                        out[i] = body
                        replaced = True
                    else:
                        changes.append(
                            {"was": ln, "now": "(removed duplicate spring onion line)"}
                        )
                        replaced = True
                    break
                if not replaced:
                    changes.append(
                        {"was": ln, "now": "(removed duplicate spring onion line)"}
                    )
                continue
            seen_cores.add(key)
            out.append(body)  # strip stale FODMAP tags; re-score later
            if body != ln:
                changes.append({"was": ln, "now": body + " (tag cleared for rescore)"})
            continue

        out.append(ln)

    # If we had mangled lines but somehow no spring range left, don't invent qty
    new_text = "\n".join(out)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == text and not changes:
        return None, []
    if new_text == text:
        # only tag clears counted as same text after strip — force if changes
        pass
    if not changes and new_text == text:
        return None, []
    return new_text, changes


def list_candidates() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, description, notes
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
          AND (
            ingredients LIKE '%spring onion green tops, large leek%'
            OR ingredients LIKE '%spring onion green tops%'
          )
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        new_ings, changes = fix_ingredients(r["ingredients"] or "")
        if new_ings and changes:
            out.append({**dict(r), "_new": new_ings, "_changes": changes})
    return out


async def post_recipe(session, headers, recipe: dict, limiter: RateLimiter) -> bool:
    await limiter.wait_turn()
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(recipe),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
        headers=headers,
        data=form,
    ) as r:
        return '"result":true' in (await r.text()).replace(" ", "")


def update_local(uid: str, t: dict) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "UPDATE recipes SET name=?, ingredients=?, description=?, notes=?, status=? WHERE uid=?",
        (t["name"], t["ingredients"], t["description"], t["notes"], "modified", uid),
    )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)

    cands = list_candidates()
    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"Fix spring onion/leek dupes | {mode} | {len(cands)}")

    for c in cands:
        safe_print(f"\n{c['name']}")
        for ch in c["_changes"][:8]:
            safe_print(f"  {ch['was'][:70]} → {ch['now'][:70]}")

    if not apply:
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.3)
    updated = failed = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for c in cands:
            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{c['uid']}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                failed += 1
                safe_print(f"FAIL missing {c['name']}")
                continue
            new_ings, changes = fix_ingredients(rec.get("ingredients") or "")
            if not new_ings:
                continue
            t = transform_recipe(
                rec.get("name") or "",
                new_ings,
                rec.get("description") or "",
                rec.get("notes") or "",
            )
            rec["name"] = t["name"]
            rec["ingredients"] = t["ingredients"]
            rec["description"] = t["description"]
            rec["notes"] = t["notes"]
            rec["hash"] = calc_hash(rec)
            ok = await post_recipe(session, headers, rec, limiter)
            if ok:
                updated += 1
                update_local(c["uid"], t)
                safe_print(f"OK {t['name']}")
            else:
                failed += 1
                safe_print(f"FAIL save {c['name']}")

        await limiter.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

    safe_print(f"Done | updated={updated} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
