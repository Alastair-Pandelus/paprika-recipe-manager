"""Drop a second, bracketed amount from ingredient lines, via the sync API.

Paprika scales only the leading amount, so "1 3/4 cups (about 230 g) flour"
goes wrong once scaled (docs/recipe-ingredient-format.md, rule 2). The leading
amount and unit are kept as they are; a bracket holding just an amount is
removed, and one holding an amount plus a note keeps the note:

  "1/8 cup (120g) whole-egg mayonnaise"  -> "1/8 cup whole-egg mayonnaise"
  "1/6 medium eggplant (340 g, diced)"   -> "1/6 medium eggplant (diced)"

Only lines that start with an amount are touched. Dry-run unless --apply.

  python scripts/tools/fix_dual_amount_lines.py --report out.tsv [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import re

from fix_qualifier_first_lines import run

LEADING = re.compile(r"^\s*(?:\d|[½⅓⅔¼¾⅛⅜⅝⅞])")
UNIT = (
    r"(?:kg|g|grams?|gr|ml|l|litres?|liters?|oz|ounces?|lbs?|pounds?|"
    r"cups?|tbsps?|tsps?|tablespoons?|teaspoons?)"
)
NUM = r"(?:\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?(?:\s+\d+\s*/\s*\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
AMOUNT = rf"(?:about\s+|approx\.?\s+|approximately\s+|~\s*)?{NUM}(?:\s*[-–]\s*{NUM})?\s*{UNIT}\.?"
# "(120g)", "(about 230 g)", "(1/3 cup)", "(340 g, diced)"
BRACKET = re.compile(rf"\s*\(\s*{AMOUNT}\s*(?P<note>[,;]\s*[^()]*)?\)", re.I)


def rewrite(line: str) -> str | None:
    if not LEADING.match(line):
        return None

    def repl(m: re.Match) -> str:
        note = (m.group("note") or "").lstrip(",; ").strip()
        return f" ({note})" if note else ""

    new = BRACKET.sub(repl, line)
    return new if new != line else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    await run(rewrite, args.report, args.apply)


if __name__ == "__main__":
    asyncio.run(main())
