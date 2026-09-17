"""
Fix duplicated direction steps stored as:
  1. Do the thing.: Do the thing.

Common from JSON-LD HowToStep where name == text and importers joined them
as "{name}: {text}".
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
PROGRESS = ROOT / "scripts" / "tools" / ".fix_dup_directions_progress.json"

# Numbered or plain line: "<body>.: <body>."  (optional trailing period on right)
DUP_LINE = re.compile(
    r"^(?P<prefix>\d+\.\s*)?(?P<body>.+?)\.:\s*(?P=body)\.?\s*$",
    re.DOTALL,
)

# Also: "<body>: <body>" when body has no trailing period on either side
DUP_LINE_NO_PERIOD = re.compile(
    r"^(?P<prefix>\d+\.\s*)?(?P<body>.+?):\s*(?P=body)\s*$",
    re.DOTALL,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def fix_direction_line(line: str) -> str:
    if not line.strip():
        return line

    # "body.: body." exact duplicate
    m = DUP_LINE.match(line)
    if m:
        prefix = m.group("prefix") or ""
        body = m.group("body").rstrip()
        if not body.endswith((".", "!", "?")):
            body = body + "."
        return f"{prefix}{body}"

    # "body: body" exact duplicate (no period before colon)
    m = DUP_LINE_NO_PERIOD.match(line)
    if m:
        prefix = m.group("prefix") or ""
        body = m.group("body").rstrip()
        return f"{prefix}{body}"

    # "short title.: short title. rest of step..." (HowToStep name prefix of text)
    m = re.match(
        r"^(?P<prefix>\d+\.\s*)?(?P<left>.+?)\.:\s*(?P<right>.+)$",
        line,
    )
    if m:
        prefix = m.group("prefix") or ""
        left = m.group("left").rstrip()
        right = m.group("right").strip()
        left_core = left.rstrip(".!? ").strip()
        right_core = right.rstrip()
        # right begins with the left title (with or without trailing punctuation)
        if (
            right_core.startswith(left)
            or right_core.startswith(left + ".")
            or right_core.startswith(left_core + ".")
            or right_core.startswith(left_core + " ")
            or right_core == left_core
        ):
            return f"{prefix}{right}"

    # Same for "title: title rest" without period-before-colon
    m = re.match(
        r"^(?P<prefix>\d+\.\s*)?(?P<left>.+?):\s*(?P<right>.+)$",
        line,
    )
    if m:
        prefix = m.group("prefix") or ""
        left = m.group("left").rstrip()
        right = m.group("right").strip()
        # Only collapse when right clearly repeats left as a prefix
        if len(left) >= 12 and (
            right.startswith(left)
            or right.startswith(left + ".")
            or right.startswith(left.rstrip(".!? ") + ".")
            or right.startswith(left.rstrip(".!? ") + " ")
        ):
            return f"{prefix}{right}"

    return line


def fix_directions(text: str) -> str | None:
    if not text:
        return None
    # Work paragraph-wise: blank-line separated blocks (steps often one para)
    # Also fix within each physical line.
    original = text
    # Normalize newlines for processing; preserve trailing newline style later
    norm = text.replace("\r\n", "\n").replace("\r", "\n")

    # First pass: line-by-line (covers standard numbered steps)
    lines = [fix_direction_line(L) for L in norm.split("\n")]
    result = "\n".join(lines)

    # Second pass: paragraphs that are single-line dups spanning weird wraps
    # (already handled if no internal newlines in the dup)

    # Third pass: if a paragraph still has "....: ...." exact half-dup
    paras = re.split(r"(\n\n+)", result)
    out_paras: list[str] = []
    for p in paras:
        if p.startswith("\n") or not p.strip():
            out_paras.append(p)
            continue
        # Collapse whole-paragraph duplicate after ".: "
        m = re.match(
            r"^(?P<prefix>\d+\.\s*)?(?P<body>.+?)\.:\s*(?P=body)\.?\s*$",
            p.strip(),
            re.DOTALL,
        )
        if m:
            prefix = m.group("prefix") or ""
            body = m.group("body").rstrip()
            if not body.endswith((".", "!", "?")):
                body += "."
            # preserve whether paragraph had trailing newline via join of parts
            out_paras.append(f"{prefix}{body}")
        else:
            out_paras.append(p)
    result = "".join(out_paras)

    if result == norm:
        return None
    if original.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    elif not original.endswith("\n") and result.endswith("\n"):
        result = result.rstrip("\n")
    return result


async def main() -> None:
    demos = [
        "1. Bring a large pot of salted water to boil.: Bring a large pot of salted water to boil.",
        "2. Proceed with the recipe.: Proceed with the recipe.",
        "Heat oil.: Heat oil.",
        "Cook pasta: Bring water to boil and cook.",  # NOT a dup
        "Build the smoky base",  # section
    ]
    safe_print("Demos:")
    for d in demos:
        safe_print(f"  {d!r}")
        safe_print(f"  -> {fix_direction_line(d)!r}")

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
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec or rec.get("in_trash"):
                done.add(uid)
                continue

            dirs = rec.get("directions") or ""
            fixed = fix_directions(dirs)
            if not fixed:
                unchanged += 1
            else:
                changed += 1
                name = rec.get("name") or uid
                # show first changed line
                for a, b in zip(dirs.splitlines(), fixed.splitlines()):
                    if a != b:
                        safe_print(f"FIX [{changed}] {name}")
                        safe_print(f"  {a[:100]}")
                        safe_print(f"  => {b[:100]}")
                        break
                if APPLY:
                    rec["directions"] = fixed
                    rec["hash"] = calc_hash(rec)
                    if await save_recipe(s, limiter, headers, rec):
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
