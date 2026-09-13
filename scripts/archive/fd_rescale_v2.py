"""Rescale FD ingredients using descending label order; robust parse of broken brackets."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html as html_lib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

ENV_PATH = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
PORTIONS = 8


def load_env() -> None:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def clean_text(s: str) -> str:
    s = html_lib.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\\u003cb\\u003e", "").replace("\\u003c/b\\u003e", "")
    # Common FD label typos
    s = s.replace("Enzymes}", "Enzymes]")
    s = s.replace("Water}", "Water]")
    s = s.replace("Sugar)", "Sugar]")
    s = s.replace("Salt)", "Salt]")
    return re.sub(r"\s+", " ", s).strip()


def extract_from_html(html: str) -> tuple[str | None, int]:
    m = re.search(r'ingredient_list\\",\\"(.*?)\\"', html)
    if not m:
        m = re.search(r'ingredients" class="prose">(.*?)</', html, re.I | re.S)
    ingredients = clean_text(m.group(1)) if m else None
    if ingredients:
        ingredients = re.split(r"(?i)for allergens|manufactured on a site", ingredients)[0]
        ingredients = clean_text(ingredients).strip(" ,.")
    servings = [int(x) for x in re.findall(r"per (\d{2,4})g", html, re.I)]
    non100 = [s for s in servings if s != 100]
    serving = max(non100) if non100 else (max(servings) if servings else 400)
    return ingredients, serving


@dataclass
class Ing:
    name: str
    pct: float | None
    assigned: float = 0.0


def split_top_level(raw: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth_paren = depth_brack = 0
    for ch in raw:
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack = max(0, depth_brack - 1)
        if ch == "," and depth_paren == 0 and depth_brack == 0:
            part = "".join(buf).strip(" ,.")
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip(" ,.")
    if tail:
        parts.append(tail)
    return parts


def parse_ingredients(raw: str) -> list[Ing]:
    raw = clean_text(raw)
    parts = split_top_level(raw)

    # If a "part" still contains an embedded (x%) mid-list due to bad brackets,
    # split on percentage markers.
    expanded: list[str] = []
    for part in parts:
        # Pattern: Name (1.2%), Next Name, ...
        embedded = list(re.finditer(r"\((\d+(?:\.\d+)?)\s*%\)", part))
        if len(embedded) <= 1:
            expanded.append(part)
            continue
        # Split into chunks ending at each %
        start = 0
        for m in embedded:
            chunk = part[start : m.end()].strip(" ,.")
            if chunk:
                expanded.append(chunk)
            start = m.end()
        rest = part[start:].strip(" ,.")
        if rest:
            # further comma-split rest
            expanded.extend(split_top_level(rest))

    items: list[Ing] = []
    for part in expanded:
        part = part.strip(" ,.")
        if not part:
            continue
        m = re.search(r"^(.*?)(?:\((\d+(?:\.\d+)?)\s*%\))\s*$", part)
        if m:
            name, pct = m.group(1).strip(" ,."), float(m.group(2))
        else:
            name, pct = part, None
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue
        if re.fullmatch(r"microbial enzymes\]?", name, re.I):
            continue
        items.append(Ing(name=name, pct=pct))
    return items


def allocate_percentages(items: list[Ing]) -> list[Ing]:
    n = len(items)
    if not n:
        return items

    upper = [100.0] * n
    lower = [0.0] * n
    for i, it in enumerate(items):
        if it.pct is None:
            continue
        for j in range(i):
            lower[j] = max(lower[j], it.pct)
        for j in range(i + 1, n):
            upper[j] = min(upper[j], it.pct)
        it.assigned = it.pct

    remaining = max(0.0, 100.0 - sum(it.assigned for it in items if it.pct is not None))

    # Contiguous unknown runs
    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if items[i].pct is None:
            j = i
            while j < n and items[j].pct is None:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1

    run_meta = []
    for a, b in runs:
        k = b - a + 1
        hi = upper[a]
        if a > 0:
            hi = min(hi, items[a - 1].assigned if items[a - 1].assigned else upper[a])
        lo = lower[b]
        if b + 1 < n and items[b + 1].pct is not None:
            lo = max(lo, items[b + 1].pct)
        # Capacity weight: earlier slots in list can hold more
        weight = max(0.05, (hi + max(lo, 0.05)) / 2) * k
        run_meta.append({"a": a, "b": b, "k": k, "hi": hi, "lo": lo, "weight": weight})

    tw = sum(r["weight"] for r in run_meta) or 1.0
    for r in run_meta:
        r["mass"] = remaining * r["weight"] / tw

    for r in run_meta:
        a, b, k, hi, lo, mass = r["a"], r["b"], r["k"], r["hi"], r["lo"], r["mass"]
        # Must not exceed: each <= previous; last >= lo; first <= hi
        max_mass = k * hi
        min_mass = k * lo
        mass = min(max_mass, max(min_mass, mass))
        r["mass"] = mass

        if k == 1:
            items[a].assigned = min(hi, max(lo, mass))
            continue

        # Geometric decrease inside run
        best_seq = None
        for pct_r in range(55, 100):
            ratio = pct_r / 100.0
            if abs(ratio - 1) < 1e-9:
                a0 = mass / k
            else:
                a0 = mass * (1 - ratio) / (1 - ratio**k)
            seq = [a0 * (ratio**j) for j in range(k)]
            if seq[0] > hi + 1e-6:
                continue
            if seq[-1] + 1e-6 < lo:
                continue
            if any(seq[j] < seq[j + 1] - 1e-9 for j in range(k - 1)):
                continue
            best_seq = seq
            break
        if best_seq is None:
            # linear from hi down to lo
            if hi < lo:
                hi = lo
            best_seq = [hi - (hi - lo) * j / (k - 1) for j in range(k)]
            s = sum(best_seq) or 1.0
            best_seq = [x * mass / s for x in best_seq]

        for j, idx in enumerate(range(a, b + 1)):
            val = best_seq[j]
            if j == 0:
                val = min(val, hi)
            else:
                val = min(val, items[a + j - 1].assigned)
            val = max(val, lo if j == k - 1 else 0.05)
            val = min(val, upper[idx])
            val = max(val, lower[idx], 0.05)
            items[idx].assigned = val

        for j in range(1, k):
            idx = a + j
            if items[idx].assigned > items[a + j - 1].assigned:
                items[idx].assigned = items[a + j - 1].assigned

    # Global non-increasing (shrink only)
    for i in range(1, n):
        if items[i].assigned > items[i - 1].assigned:
            items[i].assigned = items[i - 1].assigned

    # Renormalize unknowns to use remaining mass, then re-enforce order by shrinking
    known_total = sum(it.assigned for it in items if it.pct is not None)
    unk = [it for it in items if it.pct is None]
    unk_sum = sum(it.assigned for it in unk)
    target = max(0.0, 100.0 - known_total)
    if unk and unk_sum > 1e-9:
        scale = target / unk_sum
        for it in unk:
            it.assigned *= scale
    for i in range(1, n):
        if items[i].assigned > items[i - 1].assigned:
            items[i].assigned = items[i - 1].assigned

    return items


def format_amount(grams: float, name: str) -> str:
    if grams < 1:
        return f"pinch of {name} (~{grams:.1f} g)"
    if grams < 10:
        return f"{grams:.1f} g {name}"
    return f"{int(round(grams))} g {name}"


def build_ingredient_text(items: list[Ing], serving_g: int, portions: int) -> str:
    total_g = serving_g * portions
    lines = [
        f"Scaled for {portions} portions from Field Doctor single serve (~{serving_g} g x {portions} = {total_g} g).",
        "Unlabelled amounts inferred from UK descending-weight ingredient order "
        "(each item <= the one above and >= the one below; leftover mass shared on that basis, not equally).",
        "",
    ]
    for it in items:
        g = total_g * it.assigned / 100.0
        if it.pct is not None:
            tag = f" [{it.pct:g}% on label]"
        else:
            tag = f" [inferred ~{it.assigned:.2f}% from list order]"
        lines.append(format_amount(g, it.name) + tag)
    return "\n".join(lines)


async def main() -> None:
    load_env()
    user = os.environ["PAPRIKA_USERNAME"]
    password = os.environ["PAPRIKA_PASSWORD"]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={"email": user, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{BASE}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        updated = 0
        samples = {}
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            if FD not in (recipe.get("categories") or []):
                continue
            url = recipe.get("source_url")
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
            raw, serving = extract_from_html(html)
            if not raw:
                print("SKIP", recipe.get("name"))
                continue

            items = allocate_percentages(parse_ingredients(raw))
            text = build_ingredient_text(items, serving, PORTIONS)
            grams = [serving * PORTIONS * it.assigned / 100 for it in items]
            mono = all(grams[i] + 0.05 >= grams[i + 1] for i in range(len(grams) - 1))
            print(f"{'OK' if mono else 'BAD'} n={len(items):2d} {recipe.get('name')[:55]}")

            recipe["ingredients"] = text
            notes = recipe.get("notes") or ""
            if "Source ingredients:" in notes:
                notes = re.sub(r"Source ingredients:.*", f"Source ingredients: {raw}", notes, flags=re.S)
            else:
                notes = notes.rstrip() + f"\n\nSource ingredients: {raw}"
            recipe["notes"] = notes
            recipe["categories"] = [FD, LF]
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe["hash"] = calc_hash(recipe)

            form = aiohttp.FormData()
            form.add_field(
                "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
            )
            async with session.post(
                f"{BASE}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
            ) as r:
                if '"result":true' in (await r.text()).replace(" ", ""):
                    updated += 1

            if "Bolognese" in (recipe.get("name") or "") or "Teriyaki Salmon" in (
                recipe.get("name") or ""
            ):
                samples[recipe["name"]] = text

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("updated", updated)
        Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_ings_samples.json").write_text(
            json.dumps(samples, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
