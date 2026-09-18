"""Convert imperial ingredient quantities to grams (or ml for liquid volume).

Priority:
  1. If a gram (or ml) value is already in brackets beside lb/oz, promote that.
  2. Otherwise convert: 1 lb = 453.592 g, 1 oz = 28.35 g, 1 fl oz = 29.57 ml.

  python scripts/tools/convert_imperial_to_metric.py
  python scripts/tools/convert_imperial_to_metric.py --apply
  python scripts/tools/convert_imperial_to_metric.py --limit 20
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
from fractions import Fraction
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
OUT_DIR = Path(__file__).resolve().parent / ".convert_imperial_dryrun"

LB_G = 453.59237
OZ_G = 28.349523125
FL_OZ_ML = 29.5735295625

VULGAR = {"¼": "1/4", "½": "1/2", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}

# 1½ → 1 1/2
COMPOUND_VULGAR = re.compile(r"(\d+)\s*([¼½¾⅓⅔⅛])")

LIQUID_HINT = re.compile(
    r"\b(juice|milk|water|oil|vinegar|stock|broth|wine|cream|sauce|syrup|"
    r"soy|tamari|broth|liquid|fluid|coconut milk|passata)\b",
    re.I,
)

META_G = (
    r"(?P<meta>\d+(?:\.\d+)?)\s*(?P<meta_unit>g|grams?|ml|millilit(?:er|re)s?)"
)

# qty + imperial + optional (metric) or [metric] + rest
LINE_RE = re.compile(
    r"""^(?P<indent>\s*)
        (?P<qty>
            \d+\s+\d+\s*/\s*\d+
          | \d+\s*/\s*\d+
          | \d+[¼½¾⅓⅔⅛]
          | \d+(?:\.\d+)?
          | [¼½¾⅓⅔⅛]
        )
        (?:\s+(?P<qty2>\d+\s*/\s*\d+|\d+(?:\.\d+)?|[¼½¾⅓⅔⅛]))?
        \s*
        (?P<unit>
            fl\.?\s*oz\.?|fluid\s*ounces?
          | lbs?\.?|pounds?
          | oz\.?|ounces?
        )
        (?:
            \s*[\(\[]\s*"""
    + META_G
    + r"""\s*[\)\]]
        )?
        (?P<rest>\s*.*)$
    """,
    re.I | re.X,
)

# Primary already metric, imperial still in brackets: "30 g (4.2 oz) zucchini"
STRIP_IMPERIAL_PAREN = re.compile(
    r"""^(?P<head>\s*\d+(?:\.\d+)?\s*(?:g|kg|ml)\b)
        (?P<mid>\s*)
        \(\s*\d+(?:\.\d+)?\s*(?:oz|ounces?|lbs?|pounds?|fl\.?\s*oz)[^)]*\)
        (?P<rest>\s*.*)$
    """,
    re.I | re.X,
)

# "about 1/2 pound," / "(3/4 pound," / "1-1.5 lb" inside a line
INLINE_POUND = re.compile(
    r"""(?P<qty>
            \d+\s+\d+\s*/\s*\d+
          | \d+\s*/\s*\d+
          | \d+[¼½¾⅓⅔⅛]
          | \d+(?:\.\d+)?
          | [¼½¾⅓⅔⅛]
        )
        (?:
            \s*(?:to\s*|-)\s*
            (?P<qty2>
                \d+\s*/\s*\d+
              | \d+(?:\.\d+)?
              | [¼½¾⅓⅔⅛]
            )
        )?
        \s*(?P<unit>lbs?\.?|pounds?)
    """,
    re.I | re.X,
)

# Can-style with metric already present
CAN_RE = re.compile(
    r"""^(?P<indent>\s*)
        (?P<pre>(?:One|1|a)?\s*)
        \(?
        (?P<qty>\d+(?:\.\d+)?)\s*(?:oz|ounces?)
        (?:\s*/\s*|\s*\(|\s+)
        (?P<meta>\d+(?:\.\d+)?)\s*(?P<meta_unit>g|grams?)
        \)?
        \)?\s*
        (?P<rest>.*)$
    """,
    re.I | re.X,
)

# "1/4 (14 oz) can" → assume 400 g can when 14 oz
CAN_OZ_ONLY = re.compile(
    r"""^(?P<indent>\s*)
        (?P<pre>(?:One|1|a|1/\d+)?\s*)
        \(\s*(?P<qty>\d+(?:\.\d+)?)\s*(?:oz|ounces?)\s*\)\s*
        (?P<rest>can\b.*)
    """,
    re.I | re.X,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def parse_qty(tok: str) -> float | None:
    tok = tok.strip()
    tok = COMPOUND_VULGAR.sub(
        lambda m: f"{m.group(1)} {VULGAR[m.group(2)]}", tok
    )
    for k, v in VULGAR.items():
        tok = tok.replace(k, v)
    tok = tok.replace("⁄", "/")
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(int(m.group(1)) + Fraction(int(m.group(2)), int(m.group(3))))
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", tok)
    if m:
        return float(Fraction(int(m.group(1)), int(m.group(2))))
    try:
        return float(tok)
    except ValueError:
        return None


def fmt_metric(value: float, unit: str) -> str:
    if value >= 10:
        return f"{int(round(value))} {unit}"
    if value >= 1:
        s = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{s} {unit}"
    s = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{s} {unit}"


def _convert_imperial_qty(qty: float, unit: str, rest: str) -> tuple[float, str]:
    unit = re.sub(r"\s+", " ", unit.lower()).rstrip(".")
    is_fl = unit.startswith("fl") or unit.startswith("fluid")
    is_lb = unit.startswith("lb") or unit.startswith("pound")
    if is_fl:
        return qty * FL_OZ_ML, "ml"
    if is_lb:
        return qty * LB_G, "g"
    # oz
    if LIQUID_HINT.search(rest):
        return qty * FL_OZ_ML, "ml"
    return qty * OZ_G, "g"


def convert_line(line: str) -> str | None:
    """Return converted line, or None if unchanged."""
    if not line.strip():
        return None
    tag = ""
    mtag = re.search(
        r"(\s+(?:🟢|🟡|🟠|🔴)\s+(?:Fructose|Lactose|Fructans|Galacto-oligosaccharides|Sorbitol|Mannitol)\s+\d+%\s*)$",
        line,
    )
    body = line
    if mtag:
        tag = mtag.group(1)
        body = line[: mtag.start()]

    # Already metric primary — drop leftover imperial paren / "57 g 5 oz"
    m = STRIP_IMPERIAL_PAREN.match(body)
    if m:
        new = f"{m.group('head')}{m.group('rest')}"
        new = re.sub(r"[ \t]{2,}", " ", new).rstrip()
        return new + tag if new + tag != line else None

    m2 = re.match(
        r"^(?P<head>.*?\b\d+(?:\.\d+)?\s*(?:g|grams?|kg|ml)\b)"
        r"\s*"
        r"\(\s*(?:about\s+)?(?:\d[\d./]*\s*(?:cups?|tbsp|tsp)\s+or\s+)?"
        r"\d+(?:\.\d+)?\s*(?:to\s+\d+(?:\.\d+)?)?\s*(?:oz|ounces?)[^)]*\)"
        r"(?P<rest>\s*.*)$",
        body,
        re.I,
    )
    if m2:
        new = f"{m2.group('head')}{m2.group('rest')}"
        new = re.sub(r"[ \t]{2,}", " ", new).rstrip()
        new = re.sub(
            r"^(\s*\d+(?:\.\d+)?\s*g)\s+\d+(?:\.\d+)?\s*(?:oz|ounces?)\b",
            r"\1",
            new,
            flags=re.I,
        )
        return new + tag if new + tag != line else None

    # Do NOT promote cup+(Ng) → Ng: after 1-serve scaling, brackets are often full-batch grams.

    # Can-style with metric already present
    m = CAN_RE.match(body)
    if m and m.group("meta"):
        meta = float(m.group("meta"))
        rest = (m.group("rest") or "").strip()
        new = f"{m.group('indent')}{fmt_metric(meta, 'g')} {rest}".rstrip()
        new = re.sub(r"[ \t]{2,}", " ", new)
        return new + tag if new + tag != line else None

    # "(14 oz) can" without grams — common 400 g tin
    m = CAN_OZ_ONLY.match(body)
    if m:
        oz = float(m.group("qty"))
        grams = 400.0 if abs(oz - 14) < 0.6 else oz * OZ_G
        pre = (m.group("pre") or "").strip()
        rest = (m.group("rest") or "").strip()
        # keep fractional pre like 1/4 as count of cans → scale grams
        pre_q = parse_qty(pre) if pre and re.match(r"^[\d¼½¾⅓⅔⅛/.\s]+$", pre) else None
        if pre_q and pre_q != 1:
            grams = grams * pre_q
            pre = ""
        head = f"{pre} ".lstrip() if pre and pre_q is None else ""
        new = f"{m.group('indent')}{head}{fmt_metric(grams, 'g')} {rest}".rstrip()
        new = re.sub(r"[ \t]{2,}", " ", new)
        return new + tag if new + tag != line else None

    # "3- to 4-lb pork loin" / "3-4 lb"
    m = re.search(
        r"(?P<q1>\d+(?:\.\d+)?)\s*-\s*(?:to\s+)?(?P<q2>\d+(?:\.\d+)?)\s*-\s*(?P<unit>lbs?\.?|pounds?)",
        body,
        re.I,
    )
    if not m:
        m = re.search(
            r"(?P<q1>\d+(?:\.\d+)?)\s*-\s*(?:to\s+)?(?P<q2>\d+(?:\.\d+)?)\s*(?P<unit>lbs?\.?|pounds?)",
            body,
            re.I,
        )
    if m and not LINE_RE.match(body):
        q = (float(m.group("q1")) + float(m.group("q2"))) / 2.0
        val, u = _convert_imperial_qty(q, m.group("unit"), "")
        new_body = body[: m.start()] + fmt_metric(val, u) + body[m.end() :]
        new_body = re.sub(r"[ \t]{2,}", " ", new_body).rstrip()
        return new_body + tag if new_body + tag != line else None

    m = LINE_RE.match(body)
    if not m:
        # try converting inline "N pound" phrases in otherwise metric/count lines
        def repl_inline(mm: re.Match) -> str:
            q = parse_qty(mm.group("qty"))
            if q is None:
                return mm.group(0)
            if mm.group("qty2"):
                q2 = parse_qty(mm.group("qty2"))
                if q2 is not None:
                    # range e.g. 1-1.5 lb → average then convert? use upper mid
                    q = (q + q2) / 2.0
            val, u = _convert_imperial_qty(q, mm.group("unit"), "")
            return fmt_metric(val, u)

        new_body, n = INLINE_POUND.subn(repl_inline, body)
        if n:
            new_body = re.sub(r"[ \t]{2,}", " ", new_body).rstrip()
            return new_body + tag if new_body + tag != line else None
        return None

    qty = parse_qty(m.group("qty"))
    if qty is None or qty <= 0:
        return None
    # "1/2 14 oz cans" → half of a 14 oz can (qty * qty2 ounces)
    qty2 = m.group("qty2")
    if qty2:
        q2 = parse_qty(qty2)
        unit_l = re.sub(r"\s+", " ", m.group("unit").lower()).rstrip(".")
        if q2 and unit_l in {"oz", "ounce", "ounces"} and q2 >= 8:
            qty = qty * q2
        # else keep first qty only (portion-scale artifact like "1/4 ½ lb")
    unit = m.group("unit")
    rest = m.group("rest") or ""
    indent = m.group("indent") or ""

    if m.group("meta"):
        meta = float(m.group("meta"))
        mu = m.group("meta_unit").lower()
        out_unit = "ml" if mu.startswith("ml") or "millilit" in mu else "g"
        rest_clean = rest.lstrip()
        new = f"{indent}{fmt_metric(meta, out_unit)} {rest_clean}".rstrip()
        new = re.sub(r"[ \t]{2,}", " ", new)
        return new + tag if new + tag != line else None

    val, out_unit = _convert_imperial_qty(qty, unit, rest)
    rest_clean = rest.lstrip()
    new = f"{indent}{fmt_metric(val, out_unit)} {rest_clean}".rstrip()
    new = re.sub(r"[ \t]{2,}", " ", new)
    return new + tag if new + tag != line else None


def convert_ingredients(text: str) -> tuple[str | None, list[dict]]:
    if not text:
        return None, []
    changes = []
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        new = convert_line(line)
        if new is not None and new != line:
            changes.append({"was": line, "now": new})
            out.append(new)
        else:
            out.append(line)
    new_text = "\n".join(out)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == text:
        return None, []
    return new_text, changes


def list_recipes(limit: int = 0) -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT uid, name, ingredients, description, notes
        FROM recipes
        WHERE coalesce(in_trash, 0) = 0
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    con.close()
    out = [dict(r) for r in rows]
    if limit > 0:
        out = out[:limit]
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
        """
        UPDATE recipes
        SET name=?, ingredients=?, description=?, notes=?, status=?
        WHERE uid=?
        """,
        (
            t["name"],
            t["ingredients"],
            t["description"],
            t["notes"],
            "modified",
            uid,
        ),
    )
    con.commit()
    con.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    apply = bool(args.apply)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    recipes = list_recipes(args.limit)
    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"Convert imperial → metric | {mode} | scan {len(recipes)} recipes")

    results = []
    changed = 0
    updated = 0
    failed = 0
    sample_changes = []
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
            new_ings, changes = convert_ingredients(local.get("ingredients") or "")
            if not new_ings:
                continue
            changed += 1
            results.append(
                {
                    "uid": local["uid"],
                    "name": local["name"],
                    "n_changes": len(changes),
                    "changes": changes[:8],
                }
            )
            if len(sample_changes) < 25:
                for ch in changes[:2]:
                    sample_changes.append(
                        f"{(local['name'] or '')[:40]}: {ch['was'][:55]} → {ch['now'][:55]}"
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
                cloud_new, cloud_ch = convert_ingredients(rec.get("ingredients") or "")
                if not cloud_new:
                    continue
                # Re-annotate FODMAP tags from new gram amounts
                t2 = transform_recipe(
                    rec.get("name") or "",
                    cloud_new,
                    rec.get("description") or "",
                    rec.get("notes") or "",
                )
                rec["name"] = t2["name"]
                rec["ingredients"] = t2["ingredients"]
                rec["description"] = t2["description"]
                rec["notes"] = t2["notes"]
                rec["hash"] = calc_hash(rec)
                ok = await post_recipe(session, headers, rec, limiter)
                if ok:
                    updated += 1
                    update_local(local["uid"], t2)
                else:
                    failed += 1
                    safe_print(f"FAIL save {local['name']}")

            if i % 100 == 0 or i == len(recipes):
                safe_print(
                    f"progress {i}/{len(recipes)} changed={changed} updated={updated} failed={failed}"
                )

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    for s in sample_changes:
        safe_print(f"  {s}")

    summary = {
        "mode": mode,
        "scanned": len(recipes),
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
        f"Done | scanned={len(recipes)} changed={changed} "
        f"updated={updated if apply else 0} failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
