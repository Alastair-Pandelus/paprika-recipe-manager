"""Rescale FD ingredients; minor items as tsp/tbsp with g in brackets."""
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

# Kitchen approx: 1 tsp ≈ 5 g, 1 tbsp ≈ 15 g (liquids ≈ ml; powders approximate)
G_PER_TSP = 5.0
MINOR_MAX_G = 40.0  # below this, prefer volume units


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
    expanded: list[str] = []
    for part in parts:
        embedded = list(re.finditer(r"\((\d+(?:\.\d+)?)\s*%\)", part))
        if len(embedded) <= 1:
            expanded.append(part)
            continue
        start = 0
        for m in embedded:
            chunk = part[start : m.end()].strip(" ,.")
            if chunk:
                expanded.append(chunk)
            start = m.end()
        rest = part[start:].strip(" ,.")
        if rest:
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
        if not name or re.fullmatch(r"microbial enzymes\]?", name, re.I):
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
        weight = max(0.05, (hi + max(lo, 0.05)) / 2) * k
        run_meta.append({"a": a, "b": b, "k": k, "hi": hi, "lo": lo, "weight": weight})

    tw = sum(r["weight"] for r in run_meta) or 1.0
    for r in run_meta:
        r["mass"] = remaining * r["weight"] / tw

    for r in run_meta:
        a, b, k, hi, lo, mass = r["a"], r["b"], r["k"], r["hi"], r["lo"], r["mass"]
        max_mass = k * hi
        min_mass = k * lo
        mass = min(max_mass, max(min_mass, mass))

        if k == 1:
            items[a].assigned = min(hi, max(lo, mass))
            continue

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

    for i in range(1, n):
        if items[i].assigned > items[i - 1].assigned:
            items[i].assigned = items[i - 1].assigned

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


def fmt_g(grams: float) -> str:
    if grams < 10:
        return f"{grams:.1f} g"
    return f"{int(round(grams))} g"


def nearest_kitchen_amount(grams: float) -> tuple[str, str]:
    """Return (quantity_phrase, unit) using tsp/tbsp. 1 tsp≈5g, 1 tbsp≈15g."""
    # Prefer tbsp when roughly >= 2 tsp (10g) and closer to a tbsp multiple
    tsp = grams / G_PER_TSP

    # Common spoon fractions (in tsp units)
    candidates = [
        0.125, 0.25, 0.333, 0.5, 0.666, 0.75,
        1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 9, 10, 12,
    ]
    best = min(candidates, key=lambda c: abs(c - tsp))
    # If still tiny, use pinch
    if grams < 0.8 or best <= 0.125 and grams < 1.2:
        return "pinch", "of"

    # Use tbsp when amount is at least ~2 tsp and lands near tbsp
    use_tbsp = best >= 2.0 and (best % 3 < 0.4 or abs(best / 3 - round(best / 3)) < 0.15 or best >= 3)
    # Prefer tbsp if >= 2.5 tsp (~12.5g) for cleaner reading
    if best >= 2.5:
        tbsp = best / 3.0
        tbsp_candidates = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3]
        best_tbsp = min(tbsp_candidates, key=lambda c: abs(c - tbsp))
        # Only switch if tbsp reading is reasonably close
        if abs(best_tbsp * 3 - best) <= 0.6 or best >= 3:
            return format_spoon(best_tbsp), "tbsp"

    return format_spoon(best), "tsp"


def format_spoon(n: float) -> str:
    mapping = {
        0.125: "1/8",
        0.25: "1/4",
        0.333: "1/3",
        0.5: "1/2",
        0.666: "2/3",
        0.75: "3/4",
        1.0: "1",
        1.25: "1 1/4",
        1.5: "1 1/2",
        1.75: "1 3/4",
        2.0: "2",
        2.5: "2 1/2",
        3.0: "3",
        3.5: "3 1/2",
        4.0: "4",
        4.5: "4 1/2",
        5.0: "5",
        6.0: "6",
        7.0: "7",
        8.0: "8",
        9.0: "9",
        10.0: "10",
        12.0: "12",
    }
    # snap to key
    key = min(mapping.keys(), key=lambda k: abs(k - n))
    return mapping[key]


def format_amount(grams: float, name: str) -> tuple[str, str | None]:
    """
    Returns (display_line_without_tag, grams_note_or_None).
    For minor items: '1 tsp X' with grams_note like '5.5 g'
    For major: '800 g X' with grams_note None
    """
    # Keep bulk solids in grams always (water, rice, meat, veg, pasta, etc. large amounts)
    if grams >= MINOR_MAX_G:
        return f"{fmt_g(grams)} {name}", None

    qty, unit = nearest_kitchen_amount(grams)
    if unit == "of":
        return f"pinch of {name}", fmt_g(grams)
    return f"{qty} {unit} {name}", fmt_g(grams)


def build_ingredient_text(items: list[Ing], serving_g: int, portions: int) -> str:
    total_g = serving_g * portions
    lines = [
        f"Scaled for {portions} portions from Field Doctor single serve (~{serving_g} g x {portions} = {total_g} g).",
        "Unlabelled amounts inferred from UK descending-weight ingredient order "
        "(each item <= the one above and >= the one below; leftover mass shared on that basis, not equally).",
        "Spoon measures use ~5 g per tsp / ~15 g per tbsp (kitchen approx); exact grams shown in brackets.",
        "",
    ]
    for it in items:
        g = total_g * it.assigned / 100.0
        amount, g_note = format_amount(g, it.name)
        if it.pct is not None:
            tag = f"{it.pct:g}% on label"
        else:
            tag = f"inferred ~{it.assigned:.2f}% from list order"
        if g_note:
            tag = f"{tag}; {g_note}"
        lines.append(f"{amount} [{tag}]")
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
        sample = None
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
            print("OK", recipe.get("name")[:60])

            recipe["ingredients"] = text
            notes = recipe.get("notes") or ""
            if "Source ingredients:" in notes:
                notes = re.sub(
                    r"Source ingredients:.*", f"Source ingredients: {raw}", notes, flags=re.S
                )
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

            if "Bolognese" in (recipe.get("name") or ""):
                sample = text

        async with session.post(f"{BASE}/v2/sync/notify/", headers=headers) as r:
            await r.text()
        print("updated", updated)
        if sample:
            Path(r"C:\Users\Pandelus\AppData\Local\Temp\fd_bolognese_spoons.txt").write_text(
                sample, encoding="utf-8"
            )


if __name__ == "__main__":
    asyncio.run(main())
