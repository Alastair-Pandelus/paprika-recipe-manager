"""Redo Bolognese FODMAP trial: in-line ingredients + description summary.

- Remove FODMAP review block from notes
- Annotate tracked ingredient lines in-place (emoji + type + × green)
- Append overall stack score to end of description
- No hidden-green section, no end-of-review marker
"""
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

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

UID = "E47DB200-625A-488F-AA9B-1E4A7EBB3178"
DRYRUN = Path(__file__).resolve().parent / ".fd_single_serve_dryrun" / f"{UID}.json"
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
APPLY = "--apply" in sys.argv

BLOCK_START = "FODMAP review (1 serve)"
BLOCK_END = "— end FODMAP review —"

# Full Monash-style names (no abbreviations in display)
TYPE_NAME = {
    "FRU": "Fructose",
    "LAC": "Lactose",
    "FRT": "Fructans",
    "GOS": "Galacto-oligosaccharides",
    "SOR": "Sorbitol",
    "MAN": "Mannitol",
}
TYPE_ORDER = ("FRU", "FRT", "LAC", "GOS", "SOR", "MAN")

# (name substrings, green_g, type_code)
MAP = [
    (["penne", "pasta"], 150, "FRT"),
    (["chopped tomato", "plum tomato"], 100, "FRU"),
    (["tomato puree", "tomato paste"], 28, "FRU"),
    (["sundried"], 8, "FRU"),
    (["red pepper"], 43, "FRU"),
    (["fennel"], 48, "FRT"),
]


def emoji_for(ratio: float) -> str:
    if ratio <= 0.25:
        return "🟢"
    if ratio <= 0.50:
        return "🟡"
    if ratio <= 0.75:
        return "🟠"
    return "🔴"


def meal_emoji(ratio: float) -> str:
    if ratio <= 0.5:
        return "🟢"
    if ratio <= 1.0:
        return "🟡"
    if ratio <= 1.5:
        return "🟠"
    return "🔴"


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def strip_notes_block(notes: str) -> str:
    notes = notes or ""
    notes = re.sub(
        rf"\n*{re.escape(BLOCK_START)}.*?{re.escape(BLOCK_END)}\n*",
        "\n",
        notes,
        flags=re.S,
    )
    # also strip any orphan partial blocks
    notes = re.sub(rf"\n*{re.escape(BLOCK_START)}.*", "", notes, flags=re.S)
    return notes.strip()


def pct(ratio: float) -> int:
    return int(round(ratio * 100))


def format_tag(ftype: str, ratio: float) -> str:
    """e.g. '🟠 Fructans 60%'"""
    return f"{emoji_for(ratio)} {TYPE_NAME[ftype]} {pct(ratio)}%"


def format_meal_tag(ftype: str, ratio: float) -> str:
    return f"{meal_emoji(ratio)} {TYPE_NAME[ftype]} {pct(ratio)}%"


def strip_desc_fodmap(desc: str) -> str:
    desc = (desc or "").rstrip()
    desc = re.sub(r"\n*FODMAP:[^\n]*\s*$", "", desc)
    names = "|".join(re.escape(n) for n in TYPE_NAME.values())
    # also strip old "Excess fructose" label if present
    names_ext = names + r"|Excess fructose"
    desc = re.sub(
        rf"\n*[🟢🟡🟠🔴]\s+(?:{names_ext})\s+\d+%.*\s*$",
        "",
        desc,
        flags=re.S,
    )
    return desc.rstrip()


def strip_inline_fodmap(line: str) -> str:
    """Remove a previous in-line FODMAP annotation if re-running."""
    names = "|".join(re.escape(n) for n in TYPE_NAME.values())
    names_ext = names + r"|Excess fructose"
    line = re.sub(
        rf"\s+[🟢🟡🟠🔴]\s+(?:{names_ext})\s+\d+%\s*$",
        "",
        line,
    )
    line = re.sub(
        r"\s+(?:FRU|FRT|LAC|GOS|SOR|MAN)\s+[🟢🟡🟠🔴]\s+~\d+(?:\.\d+)?×(?:\s+\([^)]*\))?\s*$",
        "",
        line,
    )
    return line.rstrip()


def classify_line(line: str, details: list[dict]) -> tuple[str, float, float, int] | None:
    """Return (type, grams, green, ratio) for a paprika ingredient line."""
    core = strip_inline_fodmap(line).lower()
    for keys, green, ftype in MAP:
        if not any(k in core for k in keys):
            continue
        grams = None
        for it in details:
            n = (it.get("name") or "").lower()
            if any(k in n for k in keys):
                grams = float(it["grams"])
                break
        if grams is None:
            continue
        return ftype, grams, green, grams / green
    return None


def annotate_ingredients(ingredients: str, details: list[dict]) -> tuple[str, dict[str, float]]:
    stacks: dict[str, float] = {}
    out = []
    for line in (ingredients or "").splitlines():
        if not line.strip():
            continue
        clean = strip_inline_fodmap(line)
        hit = classify_line(clean, details)
        if not hit:
            out.append(clean)
            continue
        ftype, grams, green, ratio = hit
        stacks[ftype] = stacks.get(ftype, 0) + ratio
        if emoji_for(ratio) == "🟢":
            out.append(clean)
            continue
        out.append(f"{clean}  {format_tag(ftype, ratio)}")
    text = "\n".join(out).rstrip() + ("\n" if out else "")
    return text, stacks


def meal_score(stacks: dict[str, float]) -> tuple[str, list[str]]:
    drivers: list[str] = []
    worst_emoji = "🟢"
    worst_load = 0.0
    for t in TYPE_ORDER:
        load = stacks.get(t, 0)
        if load < 0.5:
            continue
        if load > worst_load:
            worst_load = load
            worst_emoji = meal_emoji(load)
        if t == "FRU":
            drivers.append("tomato")
        if t == "FRT":
            drivers.append("pasta")
    return worst_emoji, drivers


def build_desc_summary(stacks: dict[str, float]) -> str:
    parts = []
    for t in TYPE_ORDER:
        load = stacks.get(t, 0)
        if load < 0.5:
            continue
        parts.append(format_meal_tag(t, load))
    if not parts:
        return f"🟢 all tracked types near green"
    return "  ·  ".join(parts)


_TITLE_EMOJI_RE = re.compile(r"^[🟢🟡🟠🔴]\s*")


def set_title_meal_emoji(name: str, emoji: str) -> str:
    base = _TITLE_EMOJI_RE.sub("", (name or "").strip())
    return f"{emoji} {base}"


async def main() -> None:
    art = json.loads(DRYRUN.read_text(encoding="utf-8"))
    details = art.get("ingredient_detail") or []

    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    local = con.execute(
        "SELECT name, ingredients, notes, description FROM recipes WHERE uid=?",
        (UID,),
    ).fetchone()
    if not local:
        raise SystemExit("local recipe missing")

    base_ings = local["ingredients"] or art.get("ingredients_text") or ""
    new_ings, stacks = annotate_ingredients(base_ings, details)
    meal_em, _drivers = meal_score(stacks)
    new_name = set_title_meal_emoji(local["name"] or "", meal_em)
    new_notes = strip_notes_block(local["notes"] or "")
    desc_base = strip_desc_fodmap(local["description"] or "")
    summary = build_desc_summary(stacks)
    new_desc = (desc_base + "\n\n" + summary).strip() + "\n" if desc_base else summary + "\n"

    safe_print("=== TITLE ===")
    safe_print(new_name)
    safe_print("=== DESCRIPTION (tail) ===")
    safe_print(new_desc[-300:] if len(new_desc) > 300 else new_desc)

    if not APPLY:
        safe_print("Dry-run only. Pass --apply to save.")
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

        base_ings = rec.get("ingredients") or base_ings
        new_ings, stacks = annotate_ingredients(base_ings, details)
        meal_em, _drivers = meal_score(stacks)
        new_name = set_title_meal_emoji(rec.get("name") or "", meal_em)
        new_notes = strip_notes_block(rec.get("notes") or "")
        desc_base = strip_desc_fodmap(rec.get("description") or "")
        summary = build_desc_summary(stacks)
        new_desc = (
            (desc_base + "\n\n" + summary).strip() + "\n" if desc_base else summary + "\n"
        )

        rec["name"] = new_name
        rec["ingredients"] = new_ings
        rec["notes"] = new_notes
        rec["description"] = new_desc
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
        "UPDATE recipes SET name=?, ingredients=?, notes=?, description=?, status=? WHERE uid=?",
        (new_name, new_ings, new_notes, new_desc, "modified", UID),
    )
    con.commit()
    con.close()
    safe_print("Applied.")


if __name__ == "__main__":
    asyncio.run(main())
