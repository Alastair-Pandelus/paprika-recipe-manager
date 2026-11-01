"""Make ingredient lines start with their amount so Paprika can scale them (rule 1).

Rewrites, across the whole library, through the sync API (dry run unless --apply):
  "~300g noodles"                     -> "300g noodles (about)"
  "About 100 ml milk" / "Approx. …"   -> "100 ml milk (about)"
  "Up to 2 tbsp syrup"                -> "2 tbsp syrup (up to)"
  "Zest of 1/2 lemon"                 -> "1/2 lemon (zest only)"  (juice, rind, zest and juice likewise)
  "Four 6-oz salmon fillets (skin-on)"-> "4 salmon fillets (6-oz each, skin-on)"
  "- 1 stick cinnamon"                -> "1 stick cinnamon"
  "If necessary: 1-2 tbsp cornflour"  -> "1-2 tbsp cornflour (if necessary)"
  "Pure maple syrup, up to 2 tbsp"    -> "2 tbsp pure maple syrup (up to)"
  "Fresh strawberries, about 10 medium, sliced" -> "10 medium fresh strawberries, sliced (about)"
  "A package of rice noodles (225 gr)" -> "225 g rice noodles (1 package)"
  "Filo pastry (about 12 sheets)"     -> "12 sheets filo pastry (about)"
  "The green part of 6 stalks of spring onions" -> "6 stalks spring onions (green part only)"
Equipment lines (tins, cutters), label lines ("Pork (14%)") and anything else unclear are
listed in the report for a hand check, not changed.

  python scripts/tools/fix_amount_first_lines.py --report docs/amount-first-fixes.tsv [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from fix_qualifier_first_lines import TAIL_RE, run  # noqa: E402

AMT = r"(?:\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+)?(?:\s+\d+/\d+|/\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞]|\d+\s*[½⅓⅔¼¾⅛⅜⅝⅞])"
WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
         "eight": "8", "nine": "9", "ten": "10", "twelve": "12"}
UNITS = r"(?:g|gr|kg|ml|l|tsp|tbsp|teaspoons?|tablespoons?|cups?|oz|ounces?|lb|pinch|sheets?|stalks?|medium|large|small)"


def _split_tail(line: str) -> tuple[str, str]:
    t = TAIL_RE.search(line)
    return (line[: t.start()].rstrip(), line[t.start():]) if t else (line.rstrip(), "")


def rewrite(line: str) -> str | None:
    body, tail = _split_tail(line)
    s = body.strip()
    lead = body[: len(body) - len(body.lstrip())]
    if not s or re.match(rf"^{AMT}", s):
        return None
    out = None
    m = re.match(rf"^(?:~|approx\.?|approximately|about|around|roughly|circa|c\.)\s*({AMT}.*)$", s, re.I)
    if m:
        out = f"{m.group(1)} (about)"
    m = m or None
    if not out and (m := re.match(rf"^up to\s+({AMT}.*)$", s, re.I)):
        out = f"{m.group(1)} (up to)"
    if not out and (m := re.match(rf"^(?:finely\s+)?(?:grated\s+)?(?:the\s+)?(zest and juice|juice and zest|zest|juice|rind)(?:\s+(?:of|from))?\s+({AMT})\s+(.+)$", s, re.I)):
        part = m.group(1).lower().replace("juice and zest", "zest and juice")
        what = m.group(3)
        extra = ""
        if "," in what:
            what, extra = what.split(",", 1)
            extra = ", " + extra.strip()
        out = f"{m.group(2)} {what.strip()} ({part}{' only' if ' and ' not in part else ''}){extra}"
    if not out and re.search(r"\b(pan|tin|cutters?|dish|skillet|tray|mould|mold)\b", s, re.I):
        return None  # equipment, nothing to scale
    if not out and (m := re.match(r"^(one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(\d+(?:\.\d+)?)[\s-]*(oz|ounce|g)\s*(?:\[[^\]]*\]\s*)?(can|cans|packages?|packs?|tins?|jars?)?\s*(?:of\s+)?(.+)$", s, re.I)):
        n, size, unit, pack, rest = m.groups()
        count = int(WORDS[n.lower()])
        unit = "oz" if unit.lower().startswith(("oz", "ounce")) else "g"
        total = float(size) * count
        total_s = f"{total:g}"
        extra = ""
        if rest.endswith(")") and "(" in rest:
            i = rest.find("(")
            extra = rest[i + 1:-1].replace("(", "").replace(")", "")
            rest = rest[:i].strip()
        what = f"{count} {pack}" if pack else (f"{count} x {size} {unit}" if count > 1 else "")
        bracket = ", ".join(x for x in (what, extra) if x)
        out = f"{total_s} {unit} {rest}" + (f" ({bracket})" if bracket else "")
    if not out and (m := re.match(r"^(one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(\d[\w.-]*?-?inch)\s+(.+)$", s, re.I)):
        out = f"{WORDS[m.group(1).lower()]} {m.group(3)} ({m.group(2)})"
    if not out and (m := re.match(rf"^-\s+({AMT}.*)$", s)):
        out = m.group(1)
    if not out and (m := re.match(rf"^if necessary\s*:\s*({AMT}.*)$", s, re.I)):
        out = f"{m.group(1)} (if necessary)"
    if not out and (m := re.match(rf"^([A-Za-z][\w\s'-]*?),\s*(about|up to)\s+({AMT}\s*{UNITS})\s*(.*)$", s, re.I)):
        name, q, amt, rest = m.groups()
        rest = rest.strip().lstrip(",").strip()
        out = f"{amt.strip()} {name.strip().lower() if name[0].isupper() and not name.isupper() else name.strip()}{', ' + rest if rest else ''} ({q.lower()})"
    if not out and (m := re.match(r"^a (?:package|packet|pack|bag) of (.+?)\s*\((\d+)\s*(?:g|gr)\)\s*$", s, re.I)):
        out = f"{m.group(2)} g {m.group(1)} (1 package)"
    if not out and (m := re.match(rf"^filo pastry\s*\(about\s+({AMT})\s+sheets\)\s*$", s, re.I)):
        out = f"{m.group(1)} sheets filo pastry (about)"
    if not out and (m := re.match(rf"^the green part of\s+({AMT})\s+(.+)$", s, re.I)):
        out = f"{m.group(1)} {m.group(2)} (green part only)"
    if not out:
        return None
    return f"{lead}{out}{(' ' + tail.strip()) if tail.strip() else ''}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(rewrite, a.report, a.apply))


if __name__ == "__main__":
    main()
