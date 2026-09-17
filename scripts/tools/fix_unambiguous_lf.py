"""
Make Low FODMAP recipes unambiguous: remove ingredients that are listed
then marked "omit/don't use for low FODMAP", and clean related directions /
descriptions / leftover [LF edit] notes.

Skips garlic-infused oil *method* recipes (garlic is steeped then removed).
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
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import (  # noqa: E402
    RateLimiter,
    api_json,
    safe_print,
    save_recipe,
)

APPLY = "--apply" in sys.argv
PROGRESS = ROOT / "scripts" / "tools" / ".fix_unambiguous_lf_progress.json"
LF_CAT = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

# Inline / nearby omit instructions (incl. HTML entity apostrophe)
OMIT_LF = re.compile(
    r"(?i)("
    r"omit\b.{0,50}(low\s*fodmap|fodmap)|"
    r"(skip|leave\s+out|don(?:'|&\#8217;|&apos;)?t\s+use|do\s+not\s+use)\b.{0,60}"
    r"(low\s*fodmap|fodmap|if\s+you\s+are\s+eating)|"
    r"for\s+low\s*fodmap|"
    r"if\s+.{0,30}low\s*fodmap|"
    r"low\s*fodmap\s+(option|version|variation)|"
    r"to\s+be\s+low\s*fodmap"
    r")"
)

# High-FODMAP items we remove when marked omit-for-LF (not garlic-infused oil)
HF_ING = re.compile(
    r"(?i)("
    r"\bgarlic\s+powder\b|\bonion\s+powder\b|"
    r"\bgarlic\s+cloves?\b|\bcloves?\s+(?:of\s+)?garlic\b|"
    r"\bgarlic\b(?!\s*-?\s*infused)|"
    r"\bshallots?\b|"
    r"\bonion\s+flakes\b|"
    r"\bonions?\b(?!\s+powder)"
    r")"
)

# Broader omit targets sometimes paired with LF notes
OMIT_OTHER = re.compile(
    r"(?i)\b("
    r"bananas?|honey|agave|cashew\s+cream|wheat|"
    r"apple\s+cider(?!\s+vinegar)|leeks?"
    r")\b"
)

HAS_GARLIC_INFUSED = re.compile(r"(?i)garlic[\s-]*infused")
IS_INFUSION_RECIPE = re.compile(
    r"(?i)garlic[\s-]*infused\s+(olive\s+)?oil|infused\s+oil"
)

LF_CLAIM = re.compile(r"(?i)low\s*fodmap|lofo|fodmap")

LF_EDIT_BLOCK = re.compile(
    r"(?is)\n*\[LF edit\].*$"
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def unescape_basic(s: str) -> str:
    return (
        s.replace("&#8217;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )


def is_section(line: str) -> bool:
    return bool(re.fullmatch(r"\s*[^:\n]+:\s*", line))


def line_is_omit_hf(line: str) -> bool:
    """Ingredient line that both names an HF item and says omit for LF."""
    if is_section(line):
        return False
    s = unescape_basic(line)
    if HAS_GARLIC_INFUSED.search(s) and not re.search(
        r"(?i)garlic\s+powder|cloves?\s+(?:of\s+)?garlic|garlic\s+cloves?", s
    ):
        return False
    if OMIT_LF.search(s) and (HF_ING.search(s) or OMIT_OTHER.search(s)):
        return True
    # "1 clove garlic (omit for low FODMAP)" style already covered
    # also: "garlic powder (skip for low FODMAP or add LoFo garlic replacer)"
    if re.search(r"(?i)(garlic|onion|shallot).{0,40}(omit|skip|don.?t\s+use)", s) and OMIT_LF.search(s):
        return True
    return False


def strip_omit_parens(line: str) -> str | None:
    """If line is keepable but has leftover LF-omit paren noise, clean it.
    Returns None if line should be dropped entirely.
    """
    s = unescape_basic(line)
    if line_is_omit_hf(s):
        return None
    # Clean dual notes on otherwise LF lines: "(for low FODMAP, oat is good too)"
    # Keep those — they're substitutions already in place, not omit-HF.
    return line


def ensure_garlic_oil(lines: list[str], removed_garlic: bool) -> list[str]:
    if not removed_garlic:
        return lines
    blob = "\n".join(lines)
    if HAS_GARLIC_INFUSED.search(blob):
        return lines
    # Prefer converting first plain olive oil line
    out = []
    converted = False
    for L in lines:
        if (
            not converted
            and re.search(r"(?i)\b(olive\s+oil|extra\s+virgin\s+olive\s+oil)\b", L)
            and not HAS_GARLIC_INFUSED.search(L)
            and not is_section(L)
        ):
            # Replace oil type
            nl = re.sub(
                r"(?i)\b(extra\s+virgin\s+)?olive\s+oil\b",
                "garlic-infused olive oil",
                L,
                count=1,
            )
            out.append(nl)
            converted = True
        else:
            out.append(L)
    if not converted:
        out.insert(0, "1–2 tbsp garlic-infused olive oil")
    return out


def clean_directions(text: str, removed: list[str]) -> str:
    if not text:
        return text
    s = unescape_basic(text)
    # Remove "add the garlic" / "mince the garlic" type clauses carefully
    subs = [
        (r"(?i)\badd\s+the\s+garlic,\s*", "Add the "),
        (r"(?i)\badd\s+garlic,\s*", "Add "),
        (r"(?i),?\s*and\s+(?:the\s+)?garlic\b", ""),
        (r"(?i)\b(?:the\s+)?garlic,?\s+and\s+", ""),
        (r"(?i)\badd\s+(?:the\s+)?garlic\b[,.]?\s*", ""),
        (r"(?i)\b(?:mince|crush|grate|micrograte|slice|chop)\s+(?:the\s+)?garlic\b[,.]?\s*", ""),
        (r"(?i)\bgarlic\s+and\s+ginger\b", "ginger"),
        (r"(?i)\bginger\s+and\s+garlic\b", "ginger"),
        (r"(?i)\b(?:and\s+)?(?:the\s+)?shallots?\b", ""),
        (r"(?i)\bomit\s+(?:the\s+)?garlic\b[^.]*\.", ""),
        (
            r"(?i)\bfor\s+a\s+low\s*fodmap\s+version,?\s*simply\s+omit\s+the\s+garlic[^.]*\.",
            "",
        ),
        (
            r"(?i)\bif\s+whisking,?\s*micrograte\s+the\s+garlic\s+and\s+ginger\s+but\s+if\s+using\s+a\s+blender,?\s*just\s+pop\s+'?em\s+in\s+whole!",
            "Grate or blend in the ginger.",
        ),
        (r"(?i)\bolive oil\b", "garlic-infused olive oil")
        if any("garlic-infused" in (c or "").lower() for c in removed)
        else (r"(?!x)x", ""),  # no-op placeholder swapped below
        (r"[ \t]{2,}", " "),
    ]
    # Rebuild subs without the conditional no-op mess
    base = [
        (r"(?i)\badd\s+the\s+garlic,\s*", "Add the "),
        (r"(?i)\badd\s+garlic,\s*", "Add "),
        (r"(?i),?\s*and\s+(?:the\s+)?garlic\b", ""),
        (r"(?i)\b(?:the\s+)?garlic,?\s+and\s+", ""),
        (r"(?i)\badd\s+(?:the\s+)?garlic\b[,.]?\s*", ""),
        (r"(?i)\b(?:mince|crush|grate|micrograte|slice|chop)\s+(?:the\s+)?garlic\b[,.]?\s*", ""),
        (r"(?i)\bgarlic\s+and\s+ginger\b", "ginger"),
        (r"(?i)\bginger\s+and\s+garlic\b", "ginger"),
        (r"(?i)\bomit\s+(?:the\s+)?garlic\b[^.]*\.", ""),
        (
            r"(?i)\bfor\s+a\s+low\s*fodmap\s+version,?\s*simply\s+omit\s+the\s+garlic[^.]*\.",
            "",
        ),
        (
            r"(?i)\bif\s+whisking,?\s*micrograte\s+the\s+garlic\s+and\s+ginger\s+but\s+if\s+using\s+a\s+blender,?\s*just\s+pop\s+'?em\s+in\s+whole!",
            "Grate or blend in the ginger.",
        ),
        (r"(?i)\badd\s+banana,\s*", "Add "),
        (r"(?i)\bbanana,\s*", ""),
        (r"[ \t]{2,}", " "),
    ]
    for pat, repl in base:
        s = re.sub(pat, repl, s)
    # If we converted oil in ingredients, prefer garlic-infused wording in dirs
    if any("garlic-infused" in c.lower() for c in removed):
        s = re.sub(r"(?i)\b(?<!garlic-infused )olive oil\b", "garlic-infused olive oil", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\.\s*\.", ".", s)
    return s


def clean_description(text: str) -> str:
    if not text:
        return text
    s = unescape_basic(text)
    orig = s
    s = re.sub(
        r"(?i)\b(low\s*fodmap\s+or\s+regular\s+option|regular\s+or\s+low\s*fodmap\s+option)\b",
        "Low FODMAP",
        s,
    )
    s = re.sub(
        r"(?i)\bmade with eggplant, garlic, olive oil and tahini\.?\s*",
        "Made with eggplant, olive oil and tahini. ",
        s,
    )
    s = re.sub(r"(?i)\bMy recipe is also low FODMAP\.", "Low FODMAP.", s)
    # Only return changed text if meaningful dual-messaging edits applied
    if s == orig:
        return text
    return s.strip()


def clean_notes(text: str) -> str:
    if not text:
        return text
    s = LF_EDIT_BLOCK.sub("", text)
    s = re.sub(
        r"(?is)\n*Source swap notes:.*?(?=\n\n|\Z)",
        "",
        s,
    )
    if s == text:
        return text
    return s.strip()


def recipe_in_lf(rec: dict) -> bool:
    cats = rec.get("categories") or []
    for c in cats:
        if c == LF_CAT or (isinstance(c, dict) and c.get("uid") == LF_CAT):
            return True
    name = rec.get("name") or ""
    desc = rec.get("description") or ""
    return bool(LF_CLAIM.search(name) or LF_CLAIM.search(desc[:200]))


def is_infusion_method(rec: dict) -> bool:
    name = rec.get("name") or ""
    if IS_INFUSION_RECIPE.search(name):
        return True
    dirs = rec.get("directions") or ""
    # classic method: cook garlic in oil then remove cloves
    if HAS_GARLIC_INFUSED.search(name) and re.search(
        r"(?i)remove\s+(?:the\s+)?(?:garlic|cloves)", dirs
    ):
        return True
    return False


def fix_recipe(rec: dict) -> tuple[dict | None, list[str]]:
    """Return (updated rec or None, list of change notes)."""
    if is_infusion_method(rec):
        return None, []

    ings = rec.get("ingredients") or ""
    if not ings:
        return None, []

    changes: list[str] = []
    lines = ings.splitlines()
    kept: list[str] = []
    removed_garlic = False
    removed_any = False

    for L in lines:
        raw = L
        s = unescape_basic(L)
        if line_is_omit_hf(s):
            removed_any = True
            if HF_ING.search(s) and re.search(r"(?i)garlic", s):
                removed_garlic = True
            changes.append(f"drop: {raw.strip()}")
            continue
        kept.append(raw)

    # Also: LF-claimed recipes with bare garlic clove / shallot / powders
    # that have omit language elsewhere in notes/dirs/desc
    blob = unescape_basic(
        "\n".join(
            [
                rec.get("description") or "",
                rec.get("directions") or "",
                rec.get("notes") or "",
            ]
        )
    )
    has_omit_elsewhere = bool(
        re.search(
            r"(?i)(omit|skip|don.?t\s+use|leave\s+out|without).{0,40}(garlic|shallot|onion\s+powder|garlic\s+powder)",
            blob,
        )
        or re.search(
            r"(?i)(garlic|shallot|onion\s+powder).{0,40}(omit|skip|don.?t\s+use|for\s+low\s*fodmap)",
            blob,
        )
        or "[LF edit]" in (rec.get("notes") or "")
    )

    if recipe_in_lf(rec) and has_omit_elsewhere:
        new_kept = []
        for L in kept:
            s = unescape_basic(L)
            if is_section(s):
                new_kept.append(L)
                continue
            if HAS_GARLIC_INFUSED.search(s) and not re.search(
                r"(?i)garlic\s+(powder|cloves?)|cloves?\s+(?:of\s+)?garlic", s
            ):
                new_kept.append(L)
                continue
            # Drop remaining HF alliums / powders when recipe tells you to omit them
            if re.search(
                r"(?i)\b(garlic\s+powder|onion\s+powder|shallots?|"
                r"garlic\s+cloves?|cloves?\s+(?:of\s+)?garlic|"
                r"(?<![-\w])garlic(?!\s*-?\s*infused))\b",
                s,
            ):
                # keep green onion tops / scallion greens lines
                if re.search(r"(?i)(green\s+(tops?|parts?)|scallion|spring\s+onion)", s):
                    new_kept.append(L)
                    continue
                removed_any = True
                if re.search(r"(?i)garlic", s):
                    removed_garlic = True
                changes.append(f"drop (LF+omit note): {L.strip()}")
                continue
            new_kept.append(L)
        kept = new_kept

    if not removed_any and "[LF edit]" not in (rec.get("notes") or ""):
        # Still may need description/notes cleanup for dual messaging with no HF left
        desc0 = rec.get("description") or ""
        notes0 = rec.get("notes") or ""
        desc1 = clean_description(desc0)
        notes1 = clean_notes(notes0)
        if desc1 == desc0 and notes1 == notes0:
            return None, []
        rec = dict(rec)
        rec["description"] = desc1
        rec["notes"] = notes1
        changes.append("clean description/notes dual messaging")
        return rec, changes

    before_oil = "\n".join(kept)
    kept = ensure_garlic_oil(kept, removed_garlic)
    after_oil = "\n".join(kept)
    if HAS_GARLIC_INFUSED.search(after_oil) and not HAS_GARLIC_INFUSED.search(before_oil):
        changes.append("add/convert: garlic-infused olive oil")

    new_ings = "\n".join(kept)
    if ings.endswith("\n"):
        new_ings += "\n"

    rec = dict(rec)
    if new_ings != ings:
        rec["ingredients"] = new_ings

    dirs0 = rec.get("directions") or ""
    dirs1 = clean_directions(dirs0, changes)
    if dirs1 != dirs0:
        rec["directions"] = dirs1
        changes.append("clean directions")

    desc0 = rec.get("description") or ""
    desc1 = clean_description(desc0)
    if desc1 != desc0:
        rec["description"] = desc1
        changes.append("clean description")

    notes0 = rec.get("notes") or ""
    notes1 = clean_notes(notes0)
    # Drop contradictory leftover LF edit claims
    if notes1 != notes0:
        rec["notes"] = notes1
        changes.append("clean notes")

    if not changes:
        return None, []
    return rec, changes


async def main() -> None:
    # demos
    demos = [
        "1 clove of garlic (don't use if you are eating low FODMAP)",
        "1 clove of garlic (don&#8217;t use if you are eating low FODMAP)",
        "1 teaspoon garlic powder (omit for low FODMAP)",
        "1–2 tbsp garlic-infused olive oil (LF; replaces onion/garlic)",
        "1 medium banana (fresh or frozen (omit for low FODMAP!))",
        "2 tbsp olive oil",
    ]
    safe_print("Line demos (drop?):")
    for d in demos:
        safe_print(f"  {line_is_omit_hf(d)!s:5}  {d}")

    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
    elif PROGRESS.exists():
        PROGRESS.unlink()

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.45)
    changed = saved = unchanged = failed = 0
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
        index = body["result"]
        safe_print(f"Index={len(index)} MODE={'APPLY' if APPLY else 'DRY'}")

        for i, e in enumerate(index, 1):
            uid = e["uid"]
            if uid in done:
                continue
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
            )
            rec = (body or {}).get("result") or {}
            if not rec or rec.get("in_trash"):
                done.add(uid)
                continue

            fixed, changes = fix_recipe(rec)
            if not fixed:
                unchanged += 1
            else:
                changed += 1
                name = rec.get("name") or uid
                safe_print(f"FIX [{changed}] {name}")
                for c in changes[:6]:
                    safe_print(f"  • {c}")
                if APPLY:
                    fixed["hash"] = calc_hash(fixed)
                    if await save_recipe(s, limiter, headers, fixed):
                        saved += 1
                    else:
                        failed += 1
                        safe_print(f"FAIL {name}")

            done.add(uid)
            if i % 50 == 0:
                PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
                safe_print(
                    f"… {i}/{len(index)} changed={changed} saved={saved} "
                    f"unchanged={unchanged} fail={failed}"
                )

        if APPLY and saved:
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()
        PROGRESS.unlink(missing_ok=True)

    safe_print(
        f"Done. changed={changed} saved={saved} unchanged={unchanged} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
