"""Regenerate _FALLBACK_FOODS in fodmap_score_lib.py from monash_fodmap_seed.json.

Run after editing the seed so offline/fallback matching stays in sync with the DB:

  python scripts/tools/sync_fodmap_fallback_from_seed.py
  python scripts/tools/upsert_monash_fodmap.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data" / "monash_fodmap_seed.json"
LIB = ROOT / "scripts" / "tools" / "fodmap_score_lib.py"


def main() -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    if isinstance(seed, dict) and "foods" in seed:
        seed = seed["foods"]

    lines = [
        "_FALLBACK_FOODS: list[tuple[list[str], float | None, str | None, bool]] = ["
    ]
    for r in seed:
        keys = r.get("match_keys") or []
        keys_repr = json.dumps(keys, ensure_ascii=False)
        green = r.get("green_g")
        ftype = r.get("fodmap_type")
        free = bool(r.get("no_upper_limit"))
        if green is None:
            g = "None"
        elif isinstance(green, float) and green == int(green):
            g = str(int(green))
        else:
            g = str(green)
        ft = "None" if ftype is None else json.dumps(ftype)
        lines.append(f"    ({keys_repr}, {g}, {ft}, {free}),")
    lines.append("]")
    block = "\n".join(lines)

    text = LIB.read_text(encoding="utf-8")
    pat = re.compile(
        r"_FALLBACK_FOODS: list\[tuple\[list\[str\], float \| None, "
        r"str \| None, bool\]\] = \[.*?^\]",
        re.M | re.S,
    )
    if not pat.search(text):
        raise SystemExit("Could not find _FALLBACK_FOODS block in fodmap_score_lib.py")
    LIB.write_text(pat.sub(block, text, count=1), encoding="utf-8")
    print(f"Synced {len(seed)} foods → {LIB.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
