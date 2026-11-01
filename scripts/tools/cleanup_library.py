"""One-off library clean-up, through the sync API only.

1. Pack sizes: a pack/tin/jar size becomes the leading quantity
   ("1/4 (12 oz) package extra firm tofu" -> "3 oz extra firm tofu").
2. Rebuild recipes whose amounts were wrecked by the old 1-serving rescale
   from their source URL at the source's servings, following
   docs/recipe-ingredient-format.md and carrying per-line FODMAP and sat-fat
   tags over to the matching new line.
3. Typos: o-for-0 in amounts ("2oo g"), unbalanced brackets, "xathan".

Never writes the local SQLite and refuses while it has pending recipes.
Saves each recipe's current ingredients/servings to --backup before writing.
Writes a report to --report. Dry-run unless --apply.

  python scripts/tools/cleanup_library.py --report r.tsv --backup b.jsonl [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import html
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fix_dual_amount_lines import rewrite as dual_rewrite  # noqa: E402
from fix_qualifier_first_lines import rewrite as qualifier_rewrite  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)

UF = {"½": .5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": .25, "¾": .75, "⅛": .125}
NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\d*[½⅓⅔¼¾⅛]|\.\d+)"
PACK_UNIT = r"(?:kg|g|gr|grams?|ml|l|oz|ounces?|lbs?|pounds?)"
CONTAINER = (r"(?:packages?|packs?|tins?|cans?|jars?|bags?|boxes?|blocks?|"
             r"bottles?|cartons?|pouch(?:es)?)")
TAG_RE = re.compile(r"\s*[🟢🟡🟠🔴♥💚❣💔].*$")
UNITS_WORDS = {
    "cup", "cups", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon",
    "teaspoons", "g", "kg", "ml", "l", "oz", "lb", "lbs", "gr", "grams", "gram",
    "the", "and", "for", "with", "into", "about", "optional", "plus", "more",
    "small", "medium", "large", "fresh", "finely", "chopped", "or", "of",
}


def num(s: str) -> float | None:
    tot = 0.0
    for part in s.split():
        if part in UF:
            tot += UF[part]
        elif re.fullmatch(r"\d+/\d+", part):
            a, b = part.split("/")
            tot += int(a) / int(b)
        elif re.fullmatch(r"\d*\.?\d+", part):
            tot += float(part)
        elif re.fullmatch(r"\d+[½⅓⅔¼¾⅛]", part):
            tot += int(part[:-1]) + UF[part[-1]]
        else:
            return None
    return tot


def fmt_amount(v: float, unit: str) -> str:
    u = unit.lower()
    u = {"gr": "g", "gram": "g", "grams": "g", "ounce": "oz", "ounces": "oz",
         "lbs": "lb", "pound": "lb", "pounds": "lb"}.get(u, u)
    if u in ("g", "ml"):
        s = str(int(round(v)))
    else:
        s = f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{s} {u}"


# ---- 1. pack sizes ---------------------------------------------------------

PACK_PATTERNS = [
    # "1/4 (12 oz) package extra firm tofu", "3/4 (400g) tins plum tomatoes"
    re.compile(rf"^(?P<lead>\s*)(?P<n>{NUM})\s*\(\s*(?P<x>{NUM})\s*(?P<u>{PACK_UNIT})\s*\)\s*"
               rf"(?:{CONTAINER}\s+)?(?:of\s+)?(?P<rest>.+)$", re.I),
    # "1/4 can (400g) lentils"
    re.compile(rf"^(?P<lead>\s*)(?P<n>{NUM})\s+{CONTAINER}\s*\(\s*(?P<x>{NUM})\s*(?P<u>{PACK_UNIT})\s*\)\s*"
               rf"(?:of\s+)?(?P<rest>.+)$", re.I),
    # "1/4 jar of smoked red pepper (350 gr)*", "1 pack Waitrose Pad Thai (400 g)"
    re.compile(rf"^(?P<lead>\s*)(?P<n>{NUM})\s+{CONTAINER}\s+(?:of\s+)?(?P<rest>.+?)\s*"
               rf"\(\s*(?P<x>{NUM})\s*(?P<u>{PACK_UNIT})\s*\)(?P<post>.*)$", re.I),
]


WHOLE_ITEM = re.compile(
    rf"^(?P<lead>\s*)1\s+(?P<rest>[^(]+?)\s*\(\s*(?:about\s+|approx\.?\s+|~\s*)?"
    rf"(?P<x>{NUM})\s*(?P<u>{PACK_UNIT})\s*\)(?P<post>.*)$", re.I)


def pack_rewrite(line: str) -> str | None:
    body = TAG_RE.sub("", line)
    tag = line[len(body):]
    for pat in PACK_PATTERNS:
        m = pat.match(body)
        if not m:
            continue
        n, x = num(m.group("n")), num(m.group("x"))
        if n is None or x is None:
            return None
        if re.match(rf"{AMOUNT_UNIT_WORDS}\b", m.group("rest"), re.I):
            return None  # "300 (10.6 oz) g chicken": a conversion, not a pack
        post = m.groupdict().get("post") or ""
        return f"{m.group('lead')}{fmt_amount(n * x, m.group('u'))} {m.group('rest').strip()}{post}{tag}"
    # "1 boneless pork loin (about 1.6 kg)": a single whole item's weight.
    m = WHOLE_ITEM.match(body)
    if m and not re.match(rf"\s*{AMOUNT_UNIT_WORDS}\b", m.group("rest"), re.I):
        x = num(m.group("x"))
        if x is not None:
            return f"{m.group('lead')}{fmt_amount(x, m.group('u'))} {m.group('rest').strip()}{m.group('post')}{tag}"
    return None


AMOUNT_UNIT_WORDS = r"(?:cups?|tbsps?|tsps?|tablespoons?|teaspoons?|g|kg|ml|l|oz|lbs?)"


# ---- 3. typos --------------------------------------------------------------

def typo_rewrite(line: str) -> str | None:
    new = re.sub(r"\b(\d+)([oO]+)(\s*(?:g|kg|ml)\b)",
                 lambda m: m.group(1) + "0" * len(m.group(2)) + m.group(3), line)
    new = re.sub(r"\bxathan\b", "xanthan", new)
    body = TAG_RE.sub("", new)
    tag = new[len(body):]
    if "[" not in body and "]" not in body:
        opens, closes = body.count("("), body.count(")")
        m = re.search(r"\s*\((?:optional|to serve|for serving|garnish)\)\s*$", body)
        if opens == closes + 1 and m:
            # "drain (optional)" -> "drain) (optional)"
            body = body[: m.start()] + ")" + body[m.start():]
        elif opens == 0 and closes == 1 and body.rstrip().endswith(")"):
            # stray ")" left by an earlier edit: "1/4 jalapeno)"
            body = body.rstrip()[:-1]
    new = body + tag
    return new if new != line else None


def odd_brackets(line: str) -> bool:
    body = TAG_RE.sub("", line)
    return body.count("(") != body.count(")")


# ---- 2. rebuild from source ------------------------------------------------

STRIPPED_UNIT = re.compile(
    r"^\s*(?:\d+/\d+|0?\.\d+)\s+(?:pure |ground |fine |sea |kosher |white |"
    r"brown |light |dark |granulated |neutral |baking |vanilla |salt\b|sugar\b|"
    r"xanthan\b)", re.I)
DOUBLE_NUMBER = re.compile(r"^\s*(?:\d+/\d+|\d*\.\d+)\s+\d+(?![\d/])")
ML = {"cup": 240, "cups": 240, "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
      "tsp": 5, "teaspoon": 5, "teaspoons": 5}
PAIR = re.compile(rf"^\s*({NUM})\s*(cups?|tbsp|tablespoons?|tsp|teaspoons?)\s*"
                  r"\((?:about\s+|~)?([\d.]+)\s*(?:g|ml|grams?|gr)\b", re.I)


def broken_line(line: str) -> bool:
    if STRIPPED_UNIT.match(line) or DOUBLE_NUMBER.match(line):
        return True
    m = PAIR.match(line)
    if m:
        q = num(m.group(1))
        if q:
            r = float(m.group(3)) / (q * ML[m.group(2).lower()])
            return r > 4 or r < 0.25
    return False


SYNONYMS = {"cilantro": "coriander", "zucchini": "courgette", "eggplant": "aubergine",
            "scallion": "spring", "scallions": "spring", "onions": "onion",
            "bell": "pepper", "peppers": "pepper"}


def tokens(text: str) -> set[str]:
    t = re.sub(r"\([^)]*\)", " ", TAG_RE.sub("", text).lower())
    out = set()
    for w in re.findall(r"[a-zà-ÿ]+", t):
        w = SYNONYMS.get(w, w)
        if len(w) > 2 and w not in UNITS_WORDS:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


AMOUNT_UNIT = (r"(?:cups?|tbsps?|tsps?|tablespoons?|teaspoons?|kg|g|gr|grams?|ml|l|"
               r"oz|ounces?|lbs?|pounds?|pinch(?:es)?|dash(?:es)?|cans?|tins?|"
               r"packages?|packs?|jars?|bunch(?:es)?|cloves?|stalks?|sprigs?|slices?|"
               r"handfuls?|heads?|fillets?|pieces?|sheets?|knobs?)")
PREFIX = re.compile(
    rf"^\s*(?P<amt>{NUM}(?:\s*(?:-|–|to)\s*{NUM})?(?:\s+{NUM})?)\s*"
    rf"(?P<unit>{AMOUNT_UNIT}\.?(?:\s+of)?)?\s+(?P<rest>.*)$", re.I)


def split_amount(line: str) -> tuple[str, str]:
    """("1/4 cup", "pure maple syrup") from "1/4 cup pure maple syrup"."""
    m = PREFIX.match(line)
    if not m:
        return "", line.strip()
    amt = m.group("amt").strip() + (f" {m.group('unit').strip()}" if m.group("unit") else "")
    return amt, m.group("rest").strip()


def fmt_num(v: float) -> str:
    whole, frac = int(v), v - int(v)
    for d in (2, 3, 4, 6, 8, 12, 16):
        n = round(frac * d)
        if abs(frac - n / d) < 0.02:
            if n == 0:
                return str(whole)
            if n == d:
                return str(whole + 1)
            return f"{whole} {n}/{d}" if whole else f"{n}/{d}"
    return f"{v:.1f}".rstrip("0").rstrip(".")


def scale_line(line: str, factor: float) -> str | None:
    m = PREFIX.match(line)
    if not m:
        return None
    q = num(m.group("amt")) if not re.search(r"-|–|to", m.group("amt")) else None
    if q is None:
        return None
    unit = f" {m.group('unit').strip()}" if m.group("unit") else ""
    return f"{fmt_num(q * factor)}{unit} {m.group('rest').strip()}"


def fetch_source(url: str) -> tuple[list[str], int | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        items = d if isinstance(d, list) else d.get("@graph", [d])
        for it in items:
            if "Recipe" in str(it.get("@type")) and it.get("recipeIngredient"):
                y = it.get("recipeYield")
                y = y[0] if isinstance(y, list) and y else y
                ym = re.search(r"\d+", str(y or ""))
                lines = [re.sub(r"\s+", " ", html.unescape(s)).strip(" ▢")
                         for s in it["recipeIngredient"]]
                return [l for l in lines if l], int(ym.group()) if ym else None
    return [], None


def format_line(line: str) -> str:
    for f in (qualifier_rewrite, pack_rewrite, dual_rewrite, typo_rewrite):
        n = f(line)
        if n is not None:
            line = n
    return line


UNIT_BASE = {
    "tsp": ("vol", 5), "tsps": ("vol", 5), "teaspoon": ("vol", 5), "teaspoons": ("vol", 5),
    "tbsp": ("vol", 15), "tbsps": ("vol", 15), "tablespoon": ("vol", 15), "tablespoons": ("vol", 15),
    "cup": ("vol", 240), "cups": ("vol", 240), "ml": ("vol", 1), "l": ("vol", 1000),
    "g": ("wt", 1), "gr": ("wt", 1), "gram": ("wt", 1), "grams": ("wt", 1), "kg": ("wt", 1000),
    "oz": ("wt", 28.35), "ounce": ("wt", 28.35), "ounces": ("wt", 28.35),
    "lb": ("wt", 453.6), "lbs": ("wt", 453.6), "pound": ("wt", 453.6), "pounds": ("wt", 453.6),
}
FODMAP_TAG = re.compile(r"[🟢🟡🟠🔴]")


def amount_value(amt: str) -> tuple[str, float, float] | None:
    """("vol", 60.0, 0.25) for "1/4 cup": family, base value, raw number."""
    m = re.match(rf"^\s*({NUM}(?:\s+{NUM})?)\s*([a-z]+)?\.?(?:\s+of)?\s*$", amt, re.I)
    if not m:
        return None
    q = num(m.group(1))
    if q is None:
        return None
    unit = (m.group(2) or "").lower()
    if not unit:
        return ("count", q, q)
    fam = UNIT_BASE.get(unit)
    return (fam[0], q * fam[1], q) if fam else (unit, q, q)


def rebuild(old_lines: list[str], src_lines: list[str], factor: int,
            was_broken: set[str], never_scaled: set[str]) -> tuple[list[str], list[str]]:
    """Undo the 1-serving rescale.

    Each line keeps its own wording and tags (the library's curated edits:
    olive oil for garlic oil, UK names). Its amount comes from the matching
    source line when the rescale broke it, rounded it to a floor (1/8), or
    when the old amount scaled back up roughly agrees with the source.
    Where the two clearly differ the old amount was a deliberate change (a
    FODMAP remediation), so it is scaled up instead. Lines with no source
    match are scaled up, except ones the rescale never touched. Exact
    duplicate lines (metric and imperial copies) keep the first."""
    src = [format_line(l) for l in src_lines]
    src_amounts = [split_amount(TAG_RE.sub("", s)) for s in src]
    src_tok = [tokens(r) for _, r in src_amounts]
    used: set[int] = set()
    seen: list[set[str]] = []
    out, notes = [], []
    for old in old_lines:
        body = TAG_RE.sub("", old)
        tag = old[len(body):]
        if not old.strip() or old.strip().endswith(":"):
            out.append(old)
            continue
        amt, rest = split_amount(body)
        ot = tokens(rest)
        subst = bool(re.search(r"\breplaces\b", body, re.I))
        if ot and not subst and ot in seen:
            notes.append(f"dropped duplicate: {old}")
            continue
        if not subst:
            seen.append(ot)

        best, score = None, 0.0
        if not subst:
            for i, st in enumerate(src_tok):
                if i in used or not st or not ot:
                    continue
                inter = len(ot & st)
                if inter / min(len(ot), len(st)) >= 0.6:
                    s = inter / len(ot | st)
                    if s > score:
                        best, score = i, s
        broken = broken_line(body) or old in was_broken
        # "1/8 1588 g pork loin": the rescale prefixed the original amount.
        m = re.match(rf"^\s*\d+/\d+\s+({NUM}\s*(?:g|kg|ml|l|oz|lbs?)\b.*)$", body, re.I)
        if m:
            if best is not None:
                used.add(best)
            out.append(format_line(m.group(1)) + tag)
            continue
        if best is not None and src_amounts[best][0]:
            s_amt = src_amounts[best][0]
            ov, sv = amount_value(amt) if amt else None, amount_value(s_amt)
            use_src, already_full = True, False
            if not (broken or old in never_scaled or ov is None or sv is None):
                if ov[0] == sv[0]:
                    if ov[2] > 0.125 and sv[1]:
                        r = ov[1] * factor / sv[1]
                        # Within 3x: the same amount. Beyond 10x: the old
                        # rescale broke it. In between: a deliberate change.
                        use_src = 0.33 <= r <= 3 or r < 0.1 or r > 10
                        # Some lines were never scaled down: already the
                        # whole-recipe amount, so they stay as written.
                        already_full = not use_src and 0.4 <= ov[1] / sv[1] <= 2.5
                elif FODMAP_TAG.search(tag) and ov[0] != "count":
                    use_src = False
            if already_full:
                used.add(best)
                out.append(format_line(body) + tag)
                continue
            if use_src:
                used.add(best)
                out.append(format_line(f"{s_amt} {rest}") + tag)
                continue
            scaled = scale_line(body, factor)
            if scaled is not None:
                used.add(best)
                notes.append(f"kept own amount (differs from source {s_amt}): {old} -> {scaled}")
                out.append(format_line(scaled) + tag)
                continue
        if broken:
            notes.append(f"broken, no source match, kept: {old}")
            out.append(old)
            continue
        if not amt or old in never_scaled:
            out.append(old)
            continue
        av = amount_value(amt)
        # A whole count ("4 spring onion green tops") was added at
        # whole-recipe size by the remediations; fractions and measured
        # amounts were scaled down with everything else.
        if av is None or (av[0] == "count" and av[2] >= 1):
            notes.append(f"not in source, kept as written: {old}")
            out.append(old)
            continue
        scaled = scale_line(body, factor)
        if scaled is None:
            notes.append(f"amount not scalable, kept: {old}")
            out.append(old)
        else:
            notes.append(f"scaled x{factor}, not in source: {old} -> {scaled}")
            out.append(format_line(scaled) + tag)
    out = [html.unescape(l) for l in out]
    return out, notes


# ---- driver ----------------------------------------------------------------

def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def local_state():
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        pending = con.execute(
            "select count(*) from recipes where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0"
        ).fetchone()[0]
        rows = con.execute(
            "select uid, name, ingredients, source_url from recipes where coalesce(in_trash,0)=0"
        ).fetchall()
    finally:
        con.close()
    return pending, rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--pack-tsv", default=str(ROOT / "docs" / "dual-amount-applied.tsv"),
                    help="before/after TSV from fix_dual_amount_lines.py")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pending, rows = local_state()
    safe_print(f"Local pending recipes: {pending}")
    if pending and args.apply:
        raise SystemExit("not applied")

    # Pack lines: the bracket was removed earlier, so map today's line back to
    # its original and rewrite that.
    pack_map: dict[str, dict[str, str]] = {}
    with open(args.pack_tsv, encoding="utf-8") as f:
        for _, uid, before, after in list(csv.reader(f, delimiter="\t"))[1:]:
            n = pack_rewrite(before)
            if n is not None:
                pack_map.setdefault(uid, {})[after] = format_line(n)

    was_broken: dict[str, set[str]] = {}
    with open(args.pack_tsv, encoding="utf-8") as f:
        for _, uid, before, after in list(csv.reader(f, delimiter="	"))[1:]:
            if broken_line(before):
                was_broken.setdefault(uid, set()).add(after)
    never_scaled: dict[str, set[str]] = {}
    q_tsv = ROOT / "docs" / "qualifier-lines-applied.tsv"
    if q_tsv.exists():
        with open(q_tsv, encoding="utf-8") as f:
            for _, uid, _, after in list(csv.reader(f, delimiter="	"))[1:]:
                never_scaled.setdefault(uid, set()).add(after)
    with open(args.pack_tsv, encoding="utf-8") as f:
        # Those lines may since have lost a bracketed amount too.
        for _, uid, before, after in list(csv.reader(f, delimiter="	"))[1:]:
            if before in never_scaled.get(uid, set()):
                never_scaled[uid].add(after)
    rebuild_uids = {uid for uid, _, ings, _ in rows
                    if any(broken_line(l) for l in (ings or "").split("\n"))}
    # Bracket pairs already removed today: use the before text from the TSV.
    with open(args.pack_tsv, encoding="utf-8") as f:
        for _, uid, before, _ in list(csv.reader(f, delimiter="\t"))[1:]:
            if broken_line(before):
                rebuild_uids.add(uid)
    edit_uids = rebuild_uids | set(pack_map) | {
        uid for uid, _, ings, _ in rows if any(typo_rewrite(l) for l in (ings or "").split("\n"))
    }
    src_by_uid = {uid: url for uid, _, _, url in rows}
    safe_print(f"To rebuild: {len(rebuild_uids)}; pack-size recipes: {len(pack_map)}; "
               f"recipes to touch: {len(edit_uids)}")

    report = []
    backup = open(args.backup, "a", encoding="utf-8") if args.apply else None
    user, pw = paprika_credentials()
    lim = RateLimiter(0.6)
    written = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(session, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for uid in sorted(edit_uids):
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, body = await api_json(session, lim, "GET", url, headers=headers)
            recipe = (body or {}).get("result")
            if st != 200 or not recipe:
                report.append([uid, "", "skip", "cloud GET failed", "", ""])
                continue
            name = recipe["name"]
            old = (recipe.get("ingredients") or "").split("\n")
            new, servings = list(old), recipe.get("servings")
            kind = []

            if uid in rebuild_uids:
                src = src_by_uid.get(uid) or recipe.get("source_url") or ""
                try:
                    src_lines, yield_n = fetch_source(src) if src else ([], None)
                except Exception as e:  # noqa: BLE001
                    src_lines, yield_n = [], None
                    safe_print(f"  source failed for {name}: {e}")
                if not src_lines or not yield_n:
                    report.append([uid, name, "needs manual rebuild",
                                   f"no usable source ({src or 'none'})", "", ""])
                else:
                    new, notes = rebuild(old, src_lines, yield_n, was_broken.get(uid, set()),
                                         never_scaled.get(uid, set()))
                    servings = str(yield_n)
                    kind.append("rebuild")
                    for n_ in notes:
                        report.append([uid, name, "note", n_, "", ""])
                    for l in new:
                        if broken_line(TAG_RE.sub("", l)):
                            report.append([uid, name, "still broken", l, "", ""])
            if "rebuild" not in kind:
                for i, l in enumerate(new):
                    n = pack_map.get(uid, {}).get(l)
                    if n is not None:
                        new[i] = n
                        kind.append("pack")
                    t = typo_rewrite(new[i])
                    if t is not None:
                        new[i] = t
                        kind.append("typo")
                    elif odd_brackets(new[i]):
                        report.append([uid, name, "odd brackets, left", new[i], "", ""])

            if new == old and servings == recipe.get("servings"):
                continue
            for a, b in zip(old, new) if "rebuild" not in kind else []:
                if a != b:
                    report.append([uid, name, "line", a, b, ""])
            if "rebuild" in kind:
                report.append([uid, name, "rebuilt", "\n".join(old), "\n".join(new),
                               f"servings {recipe.get('servings')} -> {servings}"])
            if not args.apply:
                continue
            backup.write(json.dumps({"uid": uid, "name": name, "servings": recipe.get("servings"),
                                     "ingredients": recipe.get("ingredients")}, ensure_ascii=False) + "\n")
            backup.flush()
            upd = dict(recipe, ingredients="\n".join(new), servings=servings)
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with session.post(url, headers=headers, data=form) as r:
                txt = await r.text()
                if '"result":true' not in txt.replace(" ", ""):
                    raise SystemExit(f"POST failed for {name}: {r.status} {txt[:300]}")
            written += 1

        if args.apply and written:
            await lim.wait_turn()
            async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
                safe_print("notify", r.status)
    if backup:
        backup.close()

    with open(args.report, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["uid", "recipe", "kind", "before", "after", "note"])
        w.writerows(report)
    kinds: dict[str, int] = {}
    for r in report:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    safe_print(f"Report: {kinds}" + (f"; recipes written: {written}" if args.apply else " (dry-run)"))


if __name__ == "__main__":
    asyncio.run(main())
