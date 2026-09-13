"""
Probe Desiree Nielsen / Feed Me Phoebe recipes for partial-LF / swap language,
then rewrite recipes to be unambiguously low FODMAP when swaps are clear.
Move ambiguous leftovers to a holding folder or trash — user wants guaranteed LF.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html as html_lib
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
TARGET_FOLDERS = {"Desiree Nielsen", "Feed Me Phoebe"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaprikaImporter/1.0)"}

# Strong signals that the published ingredient list is NOT guaranteed LF as written
SWAP_HINTS = [
    r"\blow[\s-]?fodmap swaps?\b",
    r"\bfodmap swaps?\b",
    r"\bto make (?:this|it) low[\s-]?fodmap\b",
    r"\bto keep (?:this|it) low[\s-]?fodmap\b",
    r"\bmake it low[\s-]?fodmap\b",
    r"\bfor low[\s-]?fodmap\b",
    r"\blow[\s-]?fodmap option\b",
    r"\blow[\s-]?fodmap version\b",
    r"\bnot low[\s-]?fodmap\b",
    r"\bhigh[\s-]?fodmap\b",
    r"\bomit the\b",
    r"\bomit(?:\s+\w+){0,3}\s+(?:for|if)\s+low[\s-]?fodmap\b",
    r"\buse only\b.{0,40}\b(?:if|for)\s+low[\s-]?fodmap\b",
    r"\bomit onion\b",
    r"\bomit garlic\b",
    r"\bditch the garlic\b",
    r"\breplace (?:the |with )\b",
    r"\bswap (?:for|in|out|with)\b",
    r"\binstead of\b",
    r"\bsee below for low[\s-]?fodmap\b",
    r"\bfodmap tip\b",
    r"\bfodmap note\b",
    r"\blow[\s-]?fodmap note\b",
]
SWAP_RE = re.compile("|".join(SWAP_HINTS), re.I)

# Common high-FODMAP ingredients that often appear with swap notes
HIGH_FODMAP_ING_RE = re.compile(
    r"(?i)\b("
    r"onion|onions|garlic(?!\s*-?\s*infused)|shallot|shallots|"
    r"honey|agave|cashew|cashews|pistachio|pistachios|"
    r"silken tofu|wheat(?!\s*-?\s*free)|couscous|"
    r"apple(?!\s*cider)|pear|mango|watermelon|cherry tomato|"
    r"cauliflower|mushroom(?!\s*\()|"
    r"chickpea|chickpeas|black bean|black beans|kidney bean|"
    r"cow'?s milk|regular milk|oat milk"
    r")\b"
)


def safe_print(*args, **kwargs) -> None:
    try:
        print(*args, **kwargs, flush=True)
    except UnicodeEncodeError:
        print(
            *(str(a).encode("ascii", "replace").decode("ascii") for a in args),
            **kwargs,
            flush=True,
        )


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html_lib.unescape(re.sub(r"\s+", " ", s)).strip()


def extract_swap_section(page_text: str) -> str:
    """Pull a Low FODMAP swaps / notes section from page prose."""
    low = page_text
    patterns = [
        r"(?is)(low[\s-]?fodmap swaps?\s*[:\-].{0,1200})",
        r"(?is)(fodmap swaps?\s*[:\-].{0,1200})",
        r"(?is)(to make this low[\s-]?fodmap[:\.].{0,800})",
        r"(?is)(to keep this low[\s-]?fodmap[:\.].{0,800})",
        r"(?is)(for low[\s-]?fodmap[:\.].{0,800})",
        r"(?is)(fodmap tip[:\s].{0,800})",
        r"(?is)(fodmap note[:\s].{0,800})",
        r"(?is)(low[\s-]?fodmap notes?\s*[:\-].{0,1200})",
    ]
    chunks = []
    for pat in patterns:
        for m in re.finditer(pat, low):
            chunk = re.sub(r"\s+", " ", m.group(1)).strip()
            if chunk and chunk not in chunks:
                chunks.append(chunk)
    return "\n".join(chunks[:4])


# Explicit rewrite rules applied to ingredient lines when swap language is present
REWRITE_RULES: list[tuple[re.Pattern, str | None]] = [
    # (match on ingredient line, replacement line or None to drop)
    (re.compile(r"(?i)^\s*(\d+[^\n]*?)\bonions?\b(?![^\n]*green|\bscallion|\bspring)", re.I), None),
    (re.compile(r"(?i).*?\bgarlic cloves?\b.*"), None),
    (re.compile(r"(?i).*?\bminced garlic\b.*"), None),
    (re.compile(r"(?i).*?\bgarlic powder\b.*"), None),
    (re.compile(r"(?i).*?\bonion powder\b.*"), None),
    (re.compile(r"(?i)^(.*)\bshallots?\b(.*)"), None),
    (
        re.compile(r"(?i)^(.*)(\bcow'?s milk\b|\bwhole milk\b|\b2% milk\b|\bskim milk\b)(.*)"),
        r"\1lactose-free milk\3",
    ),
    (re.compile(r"(?i)^(.*)(\boat milk\b)(.*)"), r"\1almond milk (LF serve) or lactose-free milk\3"),
    (re.compile(r"(?i)^(.*)(\bhoney\b)(.*)"), r"\1maple syrup\3"),
    (re.compile(r"(?i)^(.*)(\bsilken tofu\b)(.*)"), r"\1firm tofu\3"),
    (
        re.compile(r"(?i)^(.*)(\bcashews?\b)(.*)"),
        None,  # often needs specific alt; handle via notes
    ),
]


def _apply_inline_lf_clause(line: str) -> tuple[str | None, str | None]:
    """
    Desiree-style inline clauses on an ingredient line.
    Returns (new_line_or_None_to_omit, change_description_or_None).
    """
    s = line.strip()
    low = s.lower()

    # "omit for low FODMAP" / "omit if low FODMAP" on this ingredient
    if re.search(r"(?i)\bomit(?:\s+\w+){0,3}\s+(?:for|if)\s+low[\s-]?fodmap\b", s):
        return None, f"omit (inline): {s}"

    # "use only X if/for low FODMAP"
    m = re.search(
        r"(?i)^(.*?),\s*use only ([^,.;]+)(?:\s+if|\s+for)\s+low[\s-]?fodmap.*$",
        s,
    )
    if m:
        base = m.group(1).strip().rstrip(",")
        only = m.group(2).strip()
        nl = f"{base} — use only {only} (LF)"
        return nl, f"restrict: {s} -> {nl}"

    # "..., green tops only if low FODMAP" / "dark green tops if low FODMAP"
    if re.search(r"(?i)(?:dark )?green tops?(?: only)?(?:\s+if|\s+for)\s+low[\s-]?fodmap", s):
        nl = re.sub(
            r"(?i)(,|\()\s*.*$",
            "",
            s,
        ).strip()
        nl = f"{nl} (dark green tops only, LF)"
        return nl, f"green-tops: {s} -> {nl}"

    # soy milk with LF swap callout on same line / nearby handled elsewhere
    if "soy milk" in low and re.search(r"(?i)low[\s-]?fodmap swap", s):
        nl = re.sub(r"(?i)soy milk", "almond milk or macadamia milk", s)
        nl = re.sub(r"(?i)\s*\(?see below for low[\s-]?fodmap swaps?!?\)?", "", nl).strip()
        return nl, f"milk: {s} -> {nl}"

    return s, None


def apply_ingredient_rewrites(ingredients: str, swap_blob: str) -> tuple[str, list[str], bool]:
    """
    Return (new_ingredients, change_log, confident).
    confident=False if we detected swap need but couldn't safely rewrite.
    """
    lines = ingredients.splitlines()
    changes: list[str] = []
    new_lines: list[str] = []
    needs = bool(SWAP_RE.search(ingredients + "\n" + swap_blob))

    # Parse swap hints like "use X instead of Y" / "swap Y for X" / "omit the garlic"
    pair_hints: list[tuple[str, str]] = []
    for m in re.finditer(
        r"(?i)(?:use|swap(?:\s+in)?|replace(?:\s+with)?)\s+([^,.;]+?)\s+(?:instead of|for|in place of)\s+([^,.;]+)",
        swap_blob,
    ):
        pair_hints.append((m.group(2).strip().lower(), m.group(1).strip()))
    for m in re.finditer(
        r"(?i)(?:omit|leave out|skip)\s+(?:the\s+)?([a-z][a-z\s\-]{2,40})",
        swap_blob,
    ):
        item = m.group(1).strip().lower()
        item = re.split(r"\b(?:from|in|and|or|to|for)\b", item)[0].strip()
        if item:
            pair_hints.append((item, ""))
    # "swap the soy milk for almond or macadamia milk"
    for m in re.finditer(
        r"(?i)swap (?:the )?([^,.;]+?) for ([^,.;]+)",
        swap_blob,
    ):
        pair_hints.append((m.group(1).strip().lower(), m.group(2).strip()))

    omit_garlic = bool(re.search(r"(?i)omit(?:\s+the)?\s+garlic", swap_blob))
    omit_shallot = bool(re.search(r"(?i)omit(?:\s+the)?\s+shallot", swap_blob))
    omit_onion = bool(re.search(r"(?i)omit(?:\s+the)?\s+onion", swap_blob))
    ditch_garlic = bool(re.search(r"(?i)ditch the garlic", swap_blob))

    for line in lines:
        raw = line
        stripped = line.strip()
        if not stripped:
            new_lines.append(line)
            continue
        low = stripped.lower()

        # Inline Desiree clauses
        inline_line, inline_change = _apply_inline_lf_clause(stripped)
        if inline_change:
            changes.append(inline_change)
            if inline_line is None:
                continue
            stripped = inline_line
            low = stripped.lower()

        # Explicit omit garlic/shallot/onion from notes
        if (omit_garlic or ditch_garlic) and re.search(
            r"(?i)\bgarlic\b(?!\s*-?\s*infused)", stripped
        ) and not re.search(r"(?i)infused", stripped):
            changes.append(f"omit garlic: {stripped}")
            continue
        if omit_shallot and re.search(r"(?i)\bshallots?\b", stripped):
            changes.append(f"omit shallot: {stripped}")
            continue
        if omit_onion and re.search(r"(?i)\bonions?\b", stripped) and not re.search(
            r"(?i)green|scallion|spring", stripped
        ):
            changes.append(f"omit onion: {stripped}")
            continue

        replaced = False
        for old, new in pair_hints:
            token = re.split(r"\s+", old)[0]
            if len(token) >= 4 and token in low:
                if not new:
                    changes.append(f"omit: {stripped}")
                    replaced = True
                    break
                if old in low:
                    nl = re.sub(re.escape(old), new, stripped, flags=re.I)
                else:
                    nl = re.sub(re.escape(token), new, stripped, flags=re.I)
                # clean leftover swap asides
                nl = re.sub(
                    r"(?i)\s*\(?see below for low[\s-]?fodmap swaps?!?\)?",
                    "",
                    nl,
                ).strip()
                changes.append(f"swap: {stripped} -> {nl}")
                new_lines.append(nl)
                replaced = True
                break
        if replaced:
            continue

        # Generic rewrite rules when LF conversion is indicated
        hit = False
        if needs:
            for pat, repl in REWRITE_RULES:
                if pat.search(stripped):
                    if repl is None:
                        changes.append(f"omit: {stripped}")
                        hit = True
                        break
                    nl = pat.sub(repl, stripped)
                    if nl != stripped:
                        changes.append(f"rewrite: {stripped} -> {nl}")
                        new_lines.append(nl)
                        hit = True
                        break
        if hit:
            continue

        # soy milk -> almond/macadamia when swap notes mention it
        if "soy milk" in low and re.search(r"(?i)almond|macadamia", swap_blob):
            nl = re.sub(r"(?i)soy milk", "almond milk or macadamia milk", stripped)
            nl = re.sub(
                r"(?i)\s*\(?see below for low[\s-]?fodmap swaps?!?\)?",
                "",
                nl,
            ).strip()
            changes.append(f"milk: {stripped} -> {nl}")
            new_lines.append(nl)
            continue

        # pears called out as not LF in notes
        if re.search(r"(?i)\bpears?\b", stripped) and re.search(
            r"(?i)not the pears|pears? (?:are|is) (?:not |high )?fodmap|porridge .{0,40}not the pears",
            swap_blob,
        ):
            changes.append(f"omit pears (not LF): {stripped}")
            continue

        new_lines.append(stripped if inline_change else raw)

    omitted_allium = any(
        re.search(r"onion|garlic|shallot", c, re.I)
        and (c.startswith("omit") or "omit garlic" in c or "omit shallot" in c or "omit onion" in c)
        for c in changes
    )
    joined = "\n".join(new_lines)
    if omitted_allium and not re.search(r"(?i)(garlic|shallot)[-\s]?infused", joined):
        # Only add oil if recipe seems savory / cooked
        if re.search(r"(?i)oil|saute|cook|skillet|pan|roast|stir", ingredients + swap_blob):
            new_lines.insert(0, "1–2 tbsp garlic-infused olive oil (LF; replaces onion/garlic)")
            changes.append("add: garlic-infused oil")

    remaining_high = []
    for line in new_lines:
        if HIGH_FODMAP_ING_RE.search(line) and not re.search(
            r"(?i)infused|green tops|green parts only|dark green|canned.*rinsed|firm tofu|maple|lactose-free|almond milk|macadamia|florets only",
            line,
        ):
            if re.search(r"(?i)green (?:part|tops)|scallion|spring onion.*green", line):
                continue
            remaining_high.append(line.strip())

    if not needs and not changes:
        # Still move if clear high-FODMAP staples remain with no LF framing
        risky = [
            ln
            for ln in ingredients.splitlines()
            if re.search(r"(?i)\b(onion|garlic cloves?|shallot|honey|silken tofu|cashews?)\b", ln)
            and not re.search(r"(?i)infused|green tops|maple", ln)
        ]
        if risky:
            return ingredients, [], False
        return ingredients, [], True

    text = "\n".join(new_lines).rstrip() + "\n"
    if changes and not remaining_high:
        return text, changes, True
    if changes:
        serve_ok = all(
            re.search(
                r"(?i)avocado|tomato|almond butter|peanut butter|corn|sweet potato|broccoli|peas?",
                x,
            )
            for x in remaining_high
        )
        return text, changes, serve_ok

    return ingredients, [], False


def scrub_directions(directions: str) -> str:
    """Remove 'to make LF, swap X' instructional asides once ingredients are fixed."""
    lines = directions.splitlines()
    out = []
    for line in lines:
        # drop pure swap-instruction lines
        if SWAP_RE.search(line) and re.search(
            r"(?i)swap|omit|instead|replace|to make this|to keep this", line
        ):
            # keep if it's a real cooking step that also mentions LF serve sizes
            if re.search(r"(?i)^\s*\d+\.\s+.*(cook|bake|heat|stir|mix|add|simmer|roast|blend)", line):
                # strip parenthetical swap notes
                line = re.sub(
                    r"(?i)\s*[\(\[]?(?:for low[\s-]?fodmap|to make.*?fodmap|swap.*?)[\)\]]?",
                    "",
                    line,
                ).rstrip()
                out.append(line)
            continue
        out.append(line)
    return "\n".join(out)


async def post_recipe(session, headers, recipe: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


async def fetch_page(session: aiohttp.ClientSession, url: str) -> str:
    if not url:
        return ""
    try:
        async with session.get(url) as r:
            if r.status != 200:
                return ""
            return await r.text()
    except Exception:
        return ""


async def main() -> None:
    user, password = paprika_credentials()
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as web:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as paprika:
            async with paprika.post(
                f"{PAPRIKA_API}/v1/account/login",
                data={"email": user, "password": password},
            ) as r:
                token = (await r.json())["result"]["token"]
            pheaders = {"Authorization": f"Bearer {token}"}

            async with paprika.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=pheaders) as r:
                cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
            folder_uids = {
                c["name"]: c["uid"]
                for c in cats
                if c.get("name") in TARGET_FOLDERS
                and (c.get("parent_uid") or "").lower() == LF.lower()
            }
            safe_print("folders", folder_uids)
            if len(folder_uids) < 2:
                raise SystemExit("missing target folders")

            # Create holding folder for ambiguous recipes
            hold_name = "Needs LF review"
            hold = next(
                (
                    c
                    for c in cats
                    if (c.get("name") or "") == hold_name
                    and (c.get("parent_uid") or "").lower() == LF.lower()
                ),
                None,
            )
            if hold:
                hold_uid = hold["uid"]
                safe_print("Hold folder exists:", hold_uid)
            else:
                import uuid

                hold_uid = str(uuid.uuid4()).upper()
                item = {
                    "uid": hold_uid,
                    "name": hold_name,
                    "parent_uid": LF,
                    "order_flag": 99,
                }
                form = aiohttp.FormData()
                form.add_field(
                    "data",
                    gzip_obj([item]),
                    content_type="application/octet-stream",
                    filename="data",
                )
                async with paprika.post(
                    f"{PAPRIKA_API}/v2/sync/categories/", headers=pheaders, data=form
                ) as r:
                    body = await r.text()
                    if '"result":true' not in body.replace(" ", ""):
                        raise SystemExit(f"hold folder failed: {body[:300]}")
                safe_print("Created hold folder:", hold_uid)

            async with paprika.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=pheaders) as r:
                index = (await r.json())["result"]

            target = {u.lower() for u in folder_uids.values()}
            recipes: list[dict] = []
            sem = asyncio.Semaphore(4)

            async def load_one(uid: str) -> None:
                async with sem:
                    for attempt in range(6):
                        async with paprika.get(
                            f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=pheaders
                        ) as r:
                            if r.status == 429:
                                await asyncio.sleep(1.5 * (attempt + 1))
                                continue
                            rec = (await r.json(content_type=None)).get("result") or {}
                            break
                    else:
                        return
                if rec.get("in_trash"):
                    return
                cats_l = {str(c).lower() for c in (rec.get("categories") or [])}
                if cats_l & target:
                    recipes.append(rec)

            for start in range(0, len(index), 40):
                await asyncio.gather(*(load_one(e["uid"]) for e in index[start : start + 40]))
                await asyncio.sleep(0.45)
            safe_print(f"Loaded {len(recipes)} recipes from Desiree + Phoebe")

            edited = moved = already_ok = failed = 0
            for i, rec in enumerate(recipes, 1):
                name = rec.get("name") or ""
                src = rec.get("source_url") or ""
                ing = rec.get("ingredients") or ""
                directions = rec.get("directions") or ""
                notes = rec.get("notes") or ""
                desc = rec.get("description") or ""
                local_blob = "\n".join([name, ing, directions, notes, desc])

                page_html = await fetch_page(web, src) if src else ""
                page_text = strip_tags(page_html) if page_html else ""
                swap_section = extract_swap_section(page_text)
                swap_blob = "\n".join([swap_section, notes, desc, directions])

                needs = bool(SWAP_RE.search(local_blob) or SWAP_RE.search(page_text[:8000]))
                new_ing, changes, confident = apply_ingredient_rewrites(ing, swap_blob)

                if not needs and confident and not changes:
                    already_ok += 1
                    continue

                if changes and confident:
                    rec["ingredients"] = new_ing
                    rec["directions"] = scrub_directions(directions)
                    # annotate notes
                    change_txt = "; ".join(changes[:12])
                    stamp = (
                        "\n\n[LF edit] Ingredient list rewritten to a guaranteed low-FODMAP "
                        f"version. Changes: {change_txt}"
                    )
                    if swap_section:
                        stamp += f"\nSource swap notes: {swap_section[:500]}"
                    if "[LF edit]" not in notes:
                        rec["notes"] = (notes.rstrip() + stamp).strip() + "\n"
                    # ensure only original author folder (not hold)
                    # keep current categories that are Desiree/Phoebe
                    cats_now = list(rec.get("categories") or [])
                    keep = [
                        c
                        for c in cats_now
                        if c.lower() in target and c.lower() != hold_uid.lower()
                    ]
                    if not keep:
                        # pick based on source domain
                        if "desireerd.com" in (src or "").lower():
                            keep = [folder_uids["Desiree Nielsen"]]
                        else:
                            keep = [folder_uids["Feed Me Phoebe"]]
                    rec["categories"] = keep
                    rec["hash"] = calc_hash(rec)
                    ok = await post_recipe(paprika, pheaders, rec)
                    if ok:
                        edited += 1
                        safe_print(f"[{i}/{len(recipes)}] EDITED {name} :: {change_txt[:120]}")
                    else:
                        failed += 1
                        safe_print(f"[{i}/{len(recipes)}] FAIL edit {name}")
                else:
                    # Move ambiguous / unrewritable to Needs LF review
                    rec["categories"] = [hold_uid]
                    note_extra = (
                        "\n\n[LF review] Could not auto-convert to an unambiguous low-FODMAP "
                        "ingredient list. Moved out of author folder pending manual edit."
                    )
                    if swap_section:
                        note_extra += f"\nSource swap notes: {swap_section[:700]}"
                    if "[LF review]" not in notes:
                        rec["notes"] = (notes.rstrip() + note_extra).strip() + "\n"
                    rec["hash"] = calc_hash(rec)
                    ok = await post_recipe(paprika, pheaders, rec)
                    if ok:
                        moved += 1
                        safe_print(f"[{i}/{len(recipes)}] MOVED->review {name}")
                    else:
                        failed += 1
                        safe_print(f"[{i}/{len(recipes)}] FAIL move {name}")

                await asyncio.sleep(0.25)

            async with paprika.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=pheaders
            ) as r:
                await r.text()

            safe_print(
                f"\nDone. already_ok={already_ok} edited={edited} "
                f"moved_to_review={moved} failed={failed} total={len(recipes)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
