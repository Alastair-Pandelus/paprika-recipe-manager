"""Fix spring-onion / scallion / green-onion ingredient lines for UK naming + scaling.

Rules:
  - scallion(s) / green onion(s) → spring onion(s)
  - drop redundant "(spring onions)" / "(green onion)" glosses
  - "Green tops of N …" → "N spring onion green tops, …" (qty first for Paprika scale)

  python scripts/tools/fix_spring_onion_scaling.py
  python scripts/tools/fix_spring_onion_scaling.py --apply
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
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OUT_DIR = Path(__file__).resolve().parent / ".fix_spring_onion_dryrun"

# Preserve trailing FODMAP tags
TAG_RE = re.compile(
    r"(\s+(?:🟢|🟡|🟠|🔴)\s+(?:Fructose|Lactose|Fructans|Galacto-oligosaccharides|Sorbitol|Mannitol)\s+\d+%\s*)$"
)

# "Green tops of 3 to 4 scallions (spring onions), thinly sliced…"
GREEN_TOPS_OF = re.compile(
    r"""^(?P<indent>\s*)
        (?:Optional:\s*)?
        Green\s+tops?\s+of\s+
        (?P<qty>
            \d+\s*/\s*\d+
          | \d+(?:\.\d+)?
          | [¼½¾⅓⅔⅛]
          | \d+\s+\d+\s*/\s*\d+
        )
        (?:\s*(?:to|-)\s*(?P<qty2>
            \d+\s*/\s*\d+
          | \d+(?:\.\d+)?
          | [¼½¾⅓⅔⅛]
        ))?
        \s+
        (?P<body>.+)$
    """,
    re.I | re.X,
)

# "the green parts of 4 scallions"
GREEN_PARTS_OF = re.compile(
    r"""^(?P<head>.*?\b)the\s+green\s+parts?\s+of\s+
        (?P<qty>\d+(?:\.\d+)?|\d+\s*/\s*\d+|[¼½¾⅓⅔⅛])
        \s+(?P<onion>scallions?|green\s+onions?|spring\s+onions?)
        (?P<tail>\b.*)
    """,
    re.I | re.X,
)

VULGAR = {"¼": "1/4", "½": "1/2", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def normalize_onion_words(text: str) -> str:
    """UK naming + drop redundant glosses."""
    s = text
    # scallion → spring onion (preserve plural)
    s = re.sub(r"\bScallions\b", "Spring onions", s)
    s = re.sub(r"\bscallions\b", "spring onions", s)
    s = re.sub(r"\bScallion\b", "Spring onion", s)
    s = re.sub(r"\bscallion\b", "spring onion", s)
    # green onion → spring onion
    s = re.sub(r"\bGreen\s+Onions\b", "Spring onions", s)
    s = re.sub(r"\bgreen\s+onions\b", "spring onions", s)
    s = re.sub(r"\bGreen\s+Onion\b", "Spring onion", s)
    s = re.sub(r"\bgreen\s+onion\b", "spring onion", s)
    # redundant glosses after rename
    s = re.sub(
        r"\s*\(\s*spring\s+onions?\s*\)",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"\s*\(\s*green\s+onions?\s*\)",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"\s*\(\s*scallions?\s*\)",
        "",
        s,
        flags=re.I,
    )
    # "spring onion/spring onion" leftovers
    s = re.sub(
        r"\bspring\s+onion\s*/\s*spring\s+onion\b",
        "spring onion",
        s,
        flags=re.I,
    )
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+,", ",", s)
    return s


def fix_green_tops_of(line: str) -> str | None:
    m = GREEN_TOPS_OF.match(line)
    if not m:
        return None
    body = m.group("body").strip()
    # Do not rewrite leek (or other non-onion) green tops into spring onion
    if re.search(r"\bleeks?\b", body, re.I):
        return None
    if not re.search(
        r"\b(?:scallions?|green\s+onions?|spring\s+onions?)\b", body, re.I
    ):
        return None
    qty = m.group("qty").strip()
    for k, v in VULGAR.items():
        qty = qty.replace(k, v)
    if m.group("qty2"):
        qty2 = m.group("qty2").strip()
        for k, v in VULGAR.items():
            qty2 = qty2.replace(k, v)
        qty = f"{qty} to {qty2}"
    # strip leading onion word(s) — we'll use "spring onion green tops"
    body = re.sub(
        r"^(?:scallions?|green\s+onions?|spring\s+onions?)\b[,]?\s*",
        "",
        body,
        flags=re.I,
    )
    body = normalize_onion_words(body)
    # avoid doubling "green tops" / "green part only"
    body = re.sub(r"^,\s*", "", body)
    opt = "Optional: " if re.match(r"^\s*Optional:", line, re.I) else ""
    # Prefer: "4 spring onion green tops, thinly sliced…"
    new = f"{m.group('indent')}{opt}{qty} spring onion green tops"
    if body:
        if not body.startswith(",") and not body.startswith("("):
            new = f"{new}, {body}"
        else:
            new = f"{new}{body}"
    new = re.sub(r"[ \t]{2,}", " ", new).rstrip()
    # tidy ", ,"
    new = re.sub(r",\s*,+", ",", new)
    return new


def fix_line(line: str) -> str | None:
    if not line.strip():
        return None
    tag = ""
    mt = TAG_RE.search(line)
    body = line
    if mt:
        tag = mt.group(1)
        body = line[: mt.start()]

    original = body
    changed = False

    # 1) Move qty for "Green tops of N …"
    moved = fix_green_tops_of(body)
    if moved is not None:
        body = moved
        changed = True

    # 2) "the green parts of N scallions" mid-line
    def repl_parts(mm: re.Match) -> str:
        nonlocal changed
        changed = True
        return f"{mm.group('head')}{mm.group('qty')} spring onion green tops{mm.group('tail')}"

    body2, n = GREEN_PARTS_OF.subn(repl_parts, body)
    if n:
        body = body2

    # 3) Word normalisation (scallion / green onion → spring onion)
    body3 = normalize_onion_words(body)
    if body3 != body:
        body = body3
        changed = True

    # 4) "N stalks of green onion/spring onion" → "N spring onion stalks, green part only" style cleanup
    body4 = re.sub(
        r"\bstalks?\s+of\s+spring\s+onions?\b",
        "spring onion stalks",
        body,
        flags=re.I,
    )
    body4 = re.sub(
        r"\bstalks?\s+spring\s+onions?\b",
        "spring onion stalks",
        body4,
        flags=re.I,
    )
    if body4 != body:
        body = body4
        changed = True

    # Singular: "1 spring onions" → "1 spring onion"
    body5 = re.sub(r"\b1 spring onions\b", "1 spring onion", body, flags=re.I)
    if body5 != body:
        body = body5
        changed = True

    if not changed:
        return None
    new = body.rstrip() + tag
    return new if new != line else None


def fix_ingredients(text: str) -> tuple[str | None, list[dict]]:
    if not text:
        return None, []
    changes = []
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        new = fix_line(line)
        if new is not None and new != line:
            changes.append({"was": line, "now": new})
            out.append(new)
        else:
            out.append(line)
    # Drop exact duplicate lines after scallion/spring onion merge
    deduped = []
    seen: set[str] = set()
    for line in out:
        key = re.sub(r"\s+", " ", line.strip().lower())
        if key and key in seen and "spring onion" in key:
            changes.append({"was": line, "now": "(removed duplicate)"})
            continue
        if key:
            seen.add(key)
        deduped.append(line)
    new_text = "\n".join(deduped)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == text:
        return None, []
    return new_text, changes


def fix_directions(text: str) -> str | None:
    """Rename scallion/green onion in directions (no qty moves)."""
    if not text:
        return None
    new = normalize_onion_words(text)
    return new if new != text else None


def list_recipes() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, directions
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
          AND (
            ingredients LIKE '%scallion%'
            OR ingredients LIKE '%green onion%'
            OR ingredients LIKE '%Green tops of%'
            OR ingredients LIKE '%green tops of%'
            OR ingredients LIKE '%(spring onion%'
            OR ingredients LIKE '%(green onion%'
            OR directions LIKE '%scallion%'
            OR directions LIKE '%green onion%'
          )
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


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


def update_local(uid: str, ingredients: str, directions: str | None) -> None:
    con = sqlite3.connect(LOCAL_DB)
    if directions is not None:
        con.execute(
            "UPDATE recipes SET ingredients=?, directions=?, status=? WHERE uid=?",
            (ingredients, directions, "modified", uid),
        )
    else:
        con.execute(
            "UPDATE recipes SET ingredients=?, status=? WHERE uid=?",
            (ingredients, "modified", uid),
        )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = bool(args.apply)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recipes = list_recipes()
    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"Fix spring onion lines | {mode} | candidates {len(recipes)}")

    results = []
    changed = 0
    updated = 0
    failed = 0
    samples = []
    limiter = RateLimiter(0.28)
    headers = None

    if apply:
        user, pw = paprika_credentials()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        if apply:
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

        for i, local in enumerate(recipes, 1):
            new_ings, changes = fix_ingredients(local.get("ingredients") or "")
            new_dirs = fix_directions(local.get("directions") or "")
            if not new_ings and not new_dirs:
                continue
            changed += 1
            results.append(
                {
                    "uid": local["uid"],
                    "name": local["name"],
                    "n_ing": len(changes),
                    "dirs": bool(new_dirs),
                    "changes": changes[:6],
                }
            )
            if len(samples) < 20:
                for ch in changes[:2]:
                    samples.append(
                        f"{(local['name'] or '')[:36]}: {ch['was'][:50]} → {ch['now'][:50]}"
                    )

            if apply:
                assert headers is not None
                st, body = await api_json(
                    session,
                    limiter,
                    "GET",
                    f"{PAPRIKA_API}/v2/sync/recipe/{local['uid']}/",
                    headers=headers,
                )
                rec = (body or {}).get("result") or {}
                if not rec.get("uid"):
                    failed += 1
                    safe_print(f"FAIL missing {local['name']}")
                    continue
                cloud_ings, _ = fix_ingredients(rec.get("ingredients") or "")
                cloud_dirs = fix_directions(rec.get("directions") or "")
                if cloud_ings:
                    rec["ingredients"] = cloud_ings
                if cloud_dirs:
                    rec["directions"] = cloud_dirs
                if not cloud_ings and not cloud_dirs:
                    continue
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(session, headers, rec, limiter)
                if ok:
                    updated += 1
                    update_local(
                        local["uid"],
                        cloud_ings or (rec.get("ingredients") or ""),
                        cloud_dirs if cloud_dirs else None,
                    )
                else:
                    failed += 1
                    safe_print(f"FAIL save {local['name']}")

            if i % 50 == 0 or i == len(recipes):
                safe_print(
                    f"progress {i}/{len(recipes)} changed={changed} updated={updated}"
                )

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    for s in samples:
        safe_print(f"  {s}")

    summary = {
        "mode": mode,
        "candidates": len(recipes),
        "changed": changed,
        "updated": updated if apply else 0,
        "failed": failed,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recipes": results,
    }
    (OUT_DIR / "_index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    safe_print(
        f"Done | candidates={len(recipes)} changed={changed} "
        f"updated={updated if apply else 0} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
