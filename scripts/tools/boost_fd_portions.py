"""
Boost Field Doctor recipe ingredient amounts while keeping servings at 8.

  meat / fish grams  × 8/7  → round to nearest 5 g
  all other amounts  × 8/6  → grams to nearest 1 g; kitchen measures rescaled

Idempotent: skips recipes whose notes already contain the PORTION_BOOST_MARK.
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
from lib import FIELD_DOCTOR_CATEGORY_UID, PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import (  # noqa: E402
    RateLimiter,
    api_json,
    safe_print,
    save_recipe,
)

APPLY = "--apply" in sys.argv
PORTION_BOOST_MARK = "Portion boost: meat/fish ×8/7 (nearest 5g); other ×8/6 (nearest 1g)."
MEAT_FACTOR = 8.0 / 7.0
OTHER_FACTOR = 8.0 / 6.0

MEAT_FISH_RE = re.compile(
    r"(?i)\b("
    r"extra\s+lean\s+beef|"
    r"beef|chicken|turkey|pork|lamb|veal|duck|venison|"
    r"salmon|hake|cod|haddock|tuna|prawns?|shrimp|"
    r"white\s+fish|fish|meatballs?|mince"
    r")\b"
)
EXCLUDE_MEAT_CONTEXT_RE = re.compile(
    r"(?i)\b(stock|sauce|extract|broth|fat|bouillon|jus)\b"
)

# 1365 g Extra Lean Beef
RE_GRAMS = re.compile(
    r"^(\s*)(\d+(?:\.\d+)?)\s*g\b(\s+)(.*)$",
    re.IGNORECASE,
)
# 2 x 400 g tins Plum Tomato  /  1/2 x 400 g tin ...
RE_TINS = re.compile(
    r"^(\s*)(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*x\s*(\d+)\s*g\s+(tins?)\b(.*)$",
    re.IGNORECASE,
)
# 1 1/4 tbsp Tomato Puree  /  2/3 tsp ...
RE_SPOON = re.compile(
    r"^(\s*)(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s+(tbsp|tsp)\b(.*)$",
    re.IGNORECASE,
)

FRACTION_CANDIDATES = [
    0.125, 0.25, 0.333, 0.5, 0.666, 0.75,
    1, 1.25, 1.333, 1.5, 1.666, 1.75,
    2, 2.25, 2.333, 2.5, 2.666, 2.75,
    3, 3.25, 3.333, 3.5, 3.666, 3.75,
    4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 9, 10,
    12, 14, 15, 16, 18, 20, 24, 30,
]

FRACTION_LABELS = {
    0.125: "1/8",
    0.25: "1/4",
    0.333: "1/3",
    0.5: "1/2",
    0.666: "2/3",
    0.75: "3/4",
    1.0: "1",
    1.25: "1 1/4",
    1.333: "1 1/3",
    1.5: "1 1/2",
    1.666: "1 2/3",
    1.75: "1 3/4",
    2.0: "2",
    2.25: "2 1/4",
    2.333: "2 1/3",
    2.5: "2 1/2",
    2.666: "2 2/3",
    2.75: "2 3/4",
    3.0: "3",
    3.25: "3 1/4",
    3.333: "3 1/3",
    3.5: "3 1/2",
    3.666: "3 2/3",
    3.75: "3 3/4",
    4.0: "4",
    4.5: "4 1/2",
    5.0: "5",
    5.5: "5 1/2",
    6.0: "6",
    6.5: "6 1/2",
    7.0: "7",
    7.5: "7 1/2",
    8.0: "8",
    9.0: "9",
    10.0: "10",
    12.0: "12",
    14.0: "14",
    15.0: "15",
    16.0: "16",
    18.0: "18",
    20.0: "20",
    24.0: "24",
    30.0: "30",
}


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def parse_qty(text: str) -> float | None:
    s = text.strip()
    m = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.fullmatch(r"(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) / int(m.group(2))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    return None


def format_qty(n: float) -> str:
    if n <= 0:
        n = min(FRACTION_CANDIDATES)
    best = min(FRACTION_CANDIDATES, key=lambda c: abs(c - n))
    if best in FRACTION_LABELS:
        return FRACTION_LABELS[best]
    if abs(best - round(best)) < 1e-9:
        return str(int(round(best)))
    return f"{best:g}"


def is_meat_fish_line(ingredient_name: str) -> bool:
    primary = re.split(r"[\[\({]", ingredient_name, maxsplit=1)[0].strip()
    if EXCLUDE_MEAT_CONTEXT_RE.search(primary):
        return False
    return bool(MEAT_FISH_RE.search(primary))


def round_meat_g(grams: float) -> int:
    return int(5 * round(grams / 5.0))


def round_other_g(grams: float) -> int:
    return int(round(grams))


def fmt_grams(n: int) -> str:
    return f"{n} g"


def scale_line(line: str) -> tuple[str, str | None]:
    """Return (new_line, change_note_or_None)."""
    if not line.strip():
        return line, None
    if re.fullmatch(r"\s*[^:\n]+:\s*", line):
        return line, None

    m = RE_GRAMS.match(line)
    if m:
        lead, num_s, sp, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        old = float(num_s)
        meat = is_meat_fish_line(rest)
        factor = MEAT_FACTOR if meat else OTHER_FACTOR
        raw = old * factor
        new = round_meat_g(raw) if meat else round_other_g(raw)
        if old > 0 and new < 1:
            new = 1
        new_line = f"{lead}{fmt_grams(new)}{sp}{rest}"
        if new_line == line:
            return line, None
        tag = "meat×8/7→5g" if meat else "other×8/6→1g"
        return new_line, f"{tag}: {num_s} g → {new} g"

    m = RE_TINS.match(line)
    if m:
        lead, qty_s, size, tin_word, rest = (
            m.group(1),
            m.group(2),
            m.group(3),
            m.group(4),
            m.group(5),
        )
        old = parse_qty(qty_s)
        if old is None:
            return line, None
        new_q = format_qty(old * OTHER_FACTOR)
        # pluralize tin/tins from quantity roughly
        qv = parse_qty(new_q) or old * OTHER_FACTOR
        tin = "tin" if abs(qv - 1.0) < 1e-6 else ("tins" if tin_word.lower().startswith("tin") else tin_word)
        new_line = f"{lead}{new_q} x {size} g {tin}{rest}"
        if new_line == line:
            return line, None
        return new_line, f"tin×8/6: {qty_s} → {new_q}"

    m = RE_SPOON.match(line)
    if m:
        lead, qty_s, unit, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        old = parse_qty(qty_s)
        if old is None:
            return line, None
        new_q = format_qty(old * OTHER_FACTOR)
        new_line = f"{lead}{new_q} {unit}{rest}"
        if new_line == line:
            return line, None
        return new_line, f"spoon×8/6: {qty_s} {unit} → {new_q} {unit}"

    # Generic leading quantity (counts / peppers / juice) — avoid re-matching grams
    m = re.match(
        r"^(\s*)(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s+(.+)$",
        line,
    )
    if m:
        lead, qty_s, rest = m.group(1), m.group(2), m.group(3)
        # skip if rest starts with unit already handled or looks non-scalable URL-ish
        if rest.lower().startswith(("http", "g ", "g\t")):
            return line, None
        old = parse_qty(qty_s)
        if old is None:
            return line, None
        # Only scale "kitchen count" style lines (medium / peppers / juice / tbsp already done)
        if not re.match(
            r"(?i)(medium\b|green peppers?|red peppers?|yellow peppers?|orange peppers?|"
            r"lemon juice|lime juice|pinch\b)",
            rest,
        ) and not re.match(r"(?i)[a-z]", rest):
            return line, None
        # Scale any remaining leading-qty ingredient line that isn't a bare spice name without unit
        # (e.g. "1/4 Green Peppers", "2 3/4 medium Carrots")
        if re.match(r"(?i)(medium\b|.+\bpeppers?\b|.+\bjuice\b)", rest) or re.match(
            r"(?i)(green|red|yellow|orange)\s+peppers?\b", rest
        ):
            new_q = format_qty(old * OTHER_FACTOR)
            new_line = f"{lead}{new_q} {rest}"
            if new_line == line:
                return line, None
            return new_line, f"count×8/6: {qty_s} → {new_q}"

    return line, None


def boost_ingredients(text: str) -> tuple[str | None, list[str]]:
    if not text:
        return None, []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    changes: list[str] = []
    for line in lines:
        new_line, note = scale_line(line)
        out.append(new_line)
        if note:
            changes.append(f"{line.strip()}  =>  {new_line.strip()}  ({note})")
    joined = "\n".join(out)
    if text.endswith("\n") and not joined.endswith("\n"):
        joined += "\n"
    if not changes:
        return None, []
    return joined, changes


def is_fd_recipe(rec: dict) -> bool:
    if rec.get("in_trash"):
        return False
    cats = rec.get("categories") or []
    name = (rec.get("name") or "").lower()
    if FIELD_DOCTOR_CATEGORY_UID in cats:
        return True
    return "field doctor" in name


def servings_is_eight(rec: dict) -> bool:
    s = str(rec.get("servings") or "").strip().lower()
    if s in {"8", "8 servings", "8 portions"}:
        return True
    # servings_min/max sometimes used
    try:
        if int(rec.get("servings_min") or 0) == 8 and int(rec.get("servings_max") or 0) in (0, 8):
            return True
    except (TypeError, ValueError):
        pass
    return False


def already_boosted(rec: dict) -> bool:
    notes = rec.get("notes") or ""
    return PORTION_BOOST_MARK in notes


def append_boost_note(notes: str) -> str:
    notes = (notes or "").rstrip()
    if PORTION_BOOST_MARK in notes:
        return notes
    if notes:
        return notes + "\n\n" + PORTION_BOOST_MARK
    return PORTION_BOOST_MARK


async def main() -> None:
    # demos
    demos = [
        "1365 g Extra Lean Beef",
        "1505 g Chicken",
        "415 g Penne Pasta (GLUTEN FREE)",
        "1.5 g Chives",
        "6.3 g Fennel",
        "2 x 400 g tins Plum Tomato",
        "1/2 x 400 g tins Plum Tomato",
        "2 3/4 medium Carrots",
        "1/4 Green Peppers",
        "1 1/4 tbsp Tomato Puree [tomato]",
        "2/3 tsp Quinoa Flour",
        "1/3 tsp Beef Stock [Beef Extract, Water, Salt]",
        "Sauce:",
    ]
    safe_print("Demos:")
    for d in demos:
        n, note = scale_line(d)
        safe_print(f"  {d!r}")
        safe_print(f"    → {n!r}  {note or ''}")

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.4)
    changed = saved = skipped = failed = unchanged = 0

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed: {body}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers
        )
        index = body["result"]
        safe_print(f"\nIndex={len(index)} MODE={'APPLY' if APPLY else 'DRY-RUN'}")

        for i, e in enumerate(index, 1):
            st, body = await api_json(
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{e['uid']}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not is_fd_recipe(rec):
                continue
            name = rec.get("name") or e["uid"]
            if already_boosted(rec):
                skipped += 1
                safe_print(f"SKIP already boosted: {name}")
                continue
            if not servings_is_eight(rec):
                skipped += 1
                safe_print(f"SKIP servings≠8 ({rec.get('servings')!r}): {name}")
                continue

            new_ings, changes = boost_ingredients(rec.get("ingredients") or "")
            if not new_ings:
                unchanged += 1
                safe_print(f"UNCHANGED {name}")
                continue

            changed += 1
            safe_print(f"\nBOOST [{changed}] {name}")
            for c in changes[:12]:
                safe_print(f"  {c}")
            if len(changes) > 12:
                safe_print(f"  … +{len(changes) - 12} more lines")

            if APPLY:
                rec["ingredients"] = new_ings
                rec["servings"] = "8"
                rec["notes"] = append_boost_note(rec.get("notes") or "")
                rec["hash"] = calc_hash(rec)
                if await save_recipe(s, limiter, headers, rec):
                    saved += 1
                else:
                    failed += 1
                    safe_print(f"  FAIL {name}")

            if i % 100 == 0:
                safe_print(f"… scanned {i}/{len(index)}")

        if APPLY and saved:
            await limiter.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
                safe_print(f"notify: {r.status}")

    safe_print(
        f"\nDone. changed={changed} saved={saved} unchanged={unchanged} "
        f"skipped={skipped} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
