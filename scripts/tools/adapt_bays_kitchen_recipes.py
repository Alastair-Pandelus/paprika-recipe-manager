"""Adapt the Bay's Kitchen recipes (data/bays_kitchen/recipes.json) into low FODMAP, low
saturated fat Paprika drafts that use the home-made Bay's Kitchen sauces.

Rules (each change is recorded in the recipe's notes):
- the jar of sauce becomes the matching home-made sauce recipe, by weight (1 jar = 260 g);
- Bay's Kitchen concentrated stocks become low FODMAP stock; the mango chutney is left out
  (mango is high in fructose);
- leaner meat: chicken thighs to skinless breast, beef to lean steak or 5% mince, dark turkey
  to breast mince; coconut milk to light; cooking oil to 1 tsp olive oil;
- cheese and paneer halved; coconut flakes halved.

  python scripts/tools/adapt_bays_kitchen_recipes.py     # writes docs/bays-kitchen-recipe-drafts.md/.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from bays_kitchen_sauces import SAUCES as SAUCE_RECIPES  # noqa: E402

DATA = ROOT / "data" / "bays_kitchen" / "recipes.json"
OUT_MD = ROOT / "docs" / "bays-kitchen-recipe-drafts.md"
OUT_JSON = ROOT / "data" / "bays_kitchen" / "recipe_drafts.json"
JUNK = re.compile(r"^(add to cart|regular price|sale price|£|unit price|/\s*per)", re.I)
SAUCE_NAME = {h: r["name"] for h, r in SAUCE_RECIPES.items()}


END = re.compile(r"^(nutrition|step #|recipe created|copyright|stir, simmer|more recipes|not sure|emma hatcher|prep time)", re.I)
APPROX = re.compile(r"^\s*(\d+|½)\s+(small|medium|large)?\s*([^()]+?)\s*\((?:approx\.?|about|around|roughly)\s*(\d+)\s*g\)\s*(.*)$", re.I)


STEPS: list[str] = []


def clean_lines(need: str, sauce_title_words: str) -> list[str]:
    STEPS.clear()
    out: list[str] = []
    for raw in need.split("\n"):
        l = raw.strip().strip(",").strip()
        if END.match(l):
            break
        if not l or JUNK.match(l):
            continue
        if re.match(r"^\d+\.\s", l) or (len(l) > 90 and not re.match(r"^[\d½¼¾]", l)) or re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", l):
            STEPS.append(re.sub(r"^\d+\.\s*", "", l)) if len(l) > 40 else None
            continue
        if out and re.fullmatch(r"(?i)sauce|stir-in sauce", l):
            out[-1] += " Sauce"; continue
        m = APPROX.match(l)
        if m:  # rule 2: the weight becomes the amount
            count, size, name, grams, rest = m.groups()
            l = f"{grams} g {name.strip()} ({count} {size + ' ' if size else ''}".rstrip() + ")" + (f" {rest}" if rest else "")
            l = l.replace("( ", "(")
        if re.fullmatch(r"[A-Za-z][\w &'’-]* Stir-in Sauce", l, re.I) and not re.search(r"bay.?s kitchen", l, re.I):  # product card title
            continue
        if out and (l[0].islower() or l.startswith("(")):  # wrapped line
            out[-1] = out[-1].rstrip(",") + ", " + l
            continue
        out.append(l)
    return out


def jar_grams(l: str) -> str:
    m = re.match(r"^\s*(½|1/2|2|1)\s*jars?", l, re.I)
    if m:
        return {"½": "130 g", "1/2": "130 g", "1": "260 g", "2": "520 g"}[m.group(1)]
    m = re.match(r"^\s*(\d+)\s*tbsp", l, re.I)
    return f"{m.group(1)} tbsp" if m else "260 g"


_FT = ROOT / "data" / "bays_kitchen" / "fulltext6.json"
FULLTEXT = json.loads(_FT.read_text(encoding="utf-8")) if _FT.exists() else {}


def steps_from_fulltext(t: str) -> list[str]:
    """Method steps for pages that put them straight after the ingredients."""
    body = t[t.find("You'll need"):]
    end = re.search(r"Recipe created|This recipe has been|Copyright|Imogen Brady|Stir, Simmer", body)
    body = body[: end.start()] if end else body
    if re.search(r"Step #1\n\s*\S{3,}", body):
        part = body[re.search(r"Step #1\n\s*\S{3,}", body).start():]
        steps = [re.sub(r"\s*\n\s*", " ", s).strip() for s in re.split(r"Step #\d+\n", part) if s.strip()]
    else:
        steps = [l.strip() for l in body.split("\n")
                 if len(l.strip()) > 30 and not re.match(r"^[\d½¼¾]", l.strip())
                 and not re.search(r"nutrition|please note|kcal|saturates", l, re.I)]
    return [s for s in steps if len(s) > 15]


EXCLUDE = {"chicken-korma-pie"}  # the user's call: butter shortcrust, left out
# sauce handle -> sub-folder under Low Fodmap > Websites > Bay's Kitchen
SUBFOLDER = {
    "tomato-basil-stir-in-sauce": "Tomato & Basil", "mild-korma-stir-in-sauce": "Korma",
    "tikka-masala-stir-in-sauce": "Tikka Masala", "katsu-curry-stir-in-sauce": "Katsu Curry",
    "sweet-sour-stir-in-sauce": "Sweet & Sour", "thai-massaman-stir-in-sauce": "Thai Massaman",
    "thai-green-curry-stir-in-sauce": "Thai Green Curry", "spicy-arrabbiata-stir-in-sauce": "Spicy Arrabbiata",
    "thai-red-curry-stir-in-sauce": "Thai Red Curry", "teriyaki-stir-in-sauce": "Teriyaki",
}
NOT_VEGAN = re.compile(r"\b(chicken|beef|pork|turkey|prawns?|salmon|fish|cod|hake|seabass|sea bass|eggs?|cheese|mozzarella|"
                       r"cheddar|parmesan|feta|paneer|butter(?!nut)|milk|yogh?urt|honey|anchov\w*|bacon|ham)\b", re.I)


OVERRIDES = {  # handle -> [(line startswith, new line or None to leave out)]
    "chicken-parmesan-chicken-parmigiana": [("125 g lactose-free mozzarella", "60 g light mozzarella, sliced or shredded")],
    "one-pot-beef-mexican-pasta": [("56 g lactose-free", "30 g lactose-free mature Cheddar, grated")],
    "teriyaki-chicken-with-miso-sesame-greens": [("1 Tbsp Butter", "1 tsp olive oil (for the greens)")],
}


def kitchen_round(line: str) -> str:
    """Round a cut amount down to something easy to weigh: grams/ml to 5, counts to a half."""
    from fractions import Fraction
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(g|ml)\b(.*)$", line, re.I)
    if m:
        v = max(5, int(float(m.group(1)) // 5 * 5))
        return f"{v} {m.group(2).lower()}{m.group(3)}"
    m = re.match(r"^\s*(?:(\d+)\s+)?(\d+)/(\d+)\b(.*)$", line) or re.match(r"^\s*(\d+)()()\b(.*)$", line)
    if m:
        whole = int(m.group(1) or 0)
        frac = Fraction(int(m.group(2)), int(m.group(3))) if m.group(2) else Fraction(0)
        v = max(Fraction(1, 2), Fraction(int((whole + frac) * 2), 2))
        w, r = int(v), v - int(v)
        txt = (f"{w} " if w else "") + ("1/2" if r else "")
        return f"{txt.strip()}{m.group(4)}"
    return line


def stack_cut(lines: list[str], serv: str) -> tuple[list[str], list[str]]:
    """Scale the biggest contributor of any FODMAP stack over 150% so the stack fits (min 40%)."""
    from cleanup_library import scale_line
    from rescore_fodmap import tidy_amount
    cuts: list[str] = []
    n = f.parse_servings(serv) or 1
    for _ in range(4):
        stacks = f.annotate_ingredients("\n".join(lines), servings=serv)[1]
        over = [(t, v) for t, v in stacks.items() if v > 1.5]
        if not over:
            break
        ftype, total = max(over, key=lambda x: x[1])
        contrib = []
        for i, l in enumerate(lines):
            sc = f.score_ingredient_line(l)
            if sc and sc[3] == ftype and sc[2]:
                contrib.append((sc[1] / n / sc[2], i))
        if not contrib:
            break
        share, i = max(contrib)
        k = max(0.4, (share - (total - 1.45)) / share)
        scaled = scale_line(lines[i], k)
        if not scaled:
            break
        new = kitchen_round(tidy_amount(scaled))
        if new == lines[i]:
            break
        cuts.append(f"{lines[i]} to {new}")
        lines[i] = new
    return lines, cuts


def adapt(rec: dict) -> dict:
    sauce = rec["uses"][0]
    lines = clean_lines(rec["need"], sauce)
    changed: list[str] = []
    out: list[str] = []
    for l in lines:
        low = l.lower()
        new = l
        if re.search(r"bay.?s kitchen", low) and re.search(r"stir.?in|sauce", low) and "stock" not in low and "chutney" not in low:
            amt = jar_grams(l)
            note = ""
            m = re.search(r"\(([^)]*)\)", l)
            if m and "tbsp" in m.group(1).lower():
                note = f" ({m.group(1)})"
            new = f"{amt} {SAUCE_NAME[sauce]} (home-made, see that recipe){note}"
        elif "chutney" in low:
            changed.append(f"left out: {l} (mango is high in fructose)"); continue
        elif re.search(r"bay.?s kitchen.*stock|concentrated .*stock", low):
            kind = "beef" if "beef" in low else ("vegetable" if "veg" in low else "chicken")
            ml = re.search(r"(\d+)\s*ml", low)
            g = re.match(r"^\s*(\d+)\s*g\b", low)
            if ml:
                new = f"{ml.group(1)} ml low FODMAP {kind} stock (no onion or garlic)"
            elif g:
                new = f"{g.group(1)} g low FODMAP {kind} stock concentrate (no onion or garlic), made up with the water in the recipe"
            else:
                new = f"250 ml low FODMAP {kind} stock (no onion or garlic)"
            changed.append(f"{l} -> {new}")
        elif re.search(r"chicken thighs?", low) and re.match(r"^\s*\d", l):
            n = re.match(r"^\s*(\d+)\s*(?:large\s+)?(?:skinless\s+|boneless\s+|and\s+)*(?:chicken)", low)
            g = re.match(r"^\s*(\d+)\s*g", low)
            grams = g.group(1) if g else (str(int(n.group(1)) * (75 if "skin on" in low else 100)) if n else "300")
            new = f"{grams} g skinless chicken breast" + (", " + l.split(",", 1)[1].strip() if "," in l else "")
            changed.append(f"{l} -> {new} (leaner)")
        elif re.search(r"minced beef|beef mince|ground beef", low):
            new = re.sub(r"(?i)minced beef|beef mince|ground beef", "lean beef mince (5% fat)", l)
            changed.append(f"{l} -> {new} (leaner)")
        elif re.search(r"beef steak|stewing steak|braising steak|cubed beef", low):
            new = re.sub(r"(?i)(cubed )?beef steak|(stewing|braising) steak", "lean beef steak (e.g. sirloin), fat trimmed", l)
            changed.append(f"{l} -> {new} (leaner)")
        elif "turkey" in low and ("dark" in low or "ground" in low or "mince" in low):
            new = re.sub(r"(?i)ground turkey, preferably dark meat|ground turkey|turkey mince", "turkey breast mince", l)
            changed.append(f"{l} -> {new} (leaner)")
        elif re.search(r"coconut (milk|cream)", low) and not re.search(r"\blight\b", low):
            new = re.sub(r"(?i)coconut (milk|cream)", "light coconut milk", l)
            changed.append(f"{l} -> {new}")
        elif re.search(r"\b(vegetable|sunflower|rapeseed|olive|coconut|sesame|groundnut)?\s*oil\b", low) and not re.search(r"basil oil|oil spray", low) and re.match(r"^\s*[\d½¼¾]", l):
            if "sesame" in low:
                new = "1/2 tsp toasted sesame oil"
            elif "garlic" in low:
                new = "1 tsp garlic-infused olive oil"
            else:
                new = "1 tsp olive oil"
            if new.lower() != l.lower():
                changed.append(f"{l} -> {new}")
        elif re.search(r"mozzarella|cheddar|parmesan|feta|paneer|halloumi|cheese", low) and re.match(r"^\s*(\d+)\s*g", low):
            g = int(re.match(r"^\s*(\d+)\s*g", low).group(1))
            rest = re.sub(r"^\s*\d+\s*g\s*", "", l)
            if re.search(r"mozzarella|cheddar", low) and "lactose" not in low:
                rest = "lactose-free " + rest[0].lower() + rest[1:]
            new = f"{g // 2} g {rest}"
            changed.append(f"{l} -> {new} (halved, saturated fat)")
        elif "coconut flakes" in low and re.match(r"^\s*(\d+)\s*g", low):
            g = int(re.match(r"^\s*(\d+)\s*g", low).group(1))
            new = f"{g // 2} g " + re.sub(r"^\s*\d+\s*g\s*", "", l)
            changed.append(f"{l} -> {new} (halved)")
        out.append(new)
    steps = ([s.strip() for s in re.split(r"\n?Step #\d+\n", "Step #1\n" + rec["method"]) if s.strip()]
             if rec["method"].strip() else list(STEPS))
    steps = [re.sub(r"\s*\n\s*", " ", s) for s in steps]
    steps = [s for s in steps if "You'll need" not in s and not re.fullmatch(r"(Step #\d+\s*)+", s) and len(s) > 15]
    full = FULLTEXT.get(rec["url"].rsplit("/", 1)[-1])
    if full:
        steps = steps_from_fulltext(full)
    swaps = " ".join(changed).lower()
    fixed = []
    for s in steps:
        if "chicken breast" in swaps:
            s = re.sub(r"(?i)chicken thighs?", "chicken breast", s)
        if "light coconut milk" in swaps:
            s = re.sub(r"(?i)(?<!light )coconut (milk|cream)", "light coconut milk", s)
        if "lean beef" in swaps:
            s = re.sub(r"(?i)\b(minced beef|beef mince|beef cubes|beef)\b", "lean beef", s, count=1)
        fixed.append(s)
    steps = [f"Make the home-made {SAUCE_NAME[sauce]} first (see that recipe), or use a frozen portion."] + fixed
    directions = "\n\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    title = re.sub(r"\s+", " ", rec["title"]).strip()
    if "chicken breast" in " ".join(changed).lower():
        title = re.sub(r"(?i)\s*chicken thighs", " Chicken", title).strip()
    title = f"Bay's Kitchen {SUBFOLDER[sauce]} – {title}"  # the user: sauce name in every recipe name
    kind = "Soups" if re.search(r"soup", title, re.I) else ("Snacks & Bars" if re.search(r"sticks|pastry parcels|squares", title, re.I) else "Main Meals")
    nut = re.sub(r"\s*\n\s*", "; ", rec["nutrition"]).strip("; ")
    notes = [f"Adapted from Bay's Kitchen ({rec['url']}), reviewed as low FODMAP by their dietitian. For personal use.",
             f"Uses the home-made {SAUCE_NAME[sauce]} instead of the jar (lower in saturated fat, made from single spices).",
             f"Website nutrition per serving (with the jar): {nut}"]
    serv = rec.get("serves") or "2"
    handle = rec["url"].rsplit("/", 1)[-1]
    for start, repl in OVERRIDES.get(handle, []):
        idx = [i for i, l in enumerate(out) if l.lower().startswith(start.lower())]
        if not idx:
            raise SystemExit(f"override line not found in {handle}: {start}")
        old = out[idx[0]]
        if repl is None:
            del out[idx[0]]; changed.append(f"left out: {old}")
        else:
            out[idx[0]] = repl; changed.append(f"{old} -> {repl}")
    if changed:
        notes.append("Changed (low saturated fat): " + "; ".join(changed) + ".")
    out, cuts = stack_cut(out, serv)
    if cuts:
        notes.append("Low FODMAP: " + "; ".join(cuts) + " (cut so the FODMAPs that add up stay within the limit).")
    ing = "\n".join(out)
    t = f.transform_recipe(title, ing, "", "\n".join(notes), servings=serv)
    t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], "\n".join(notes), servings=serv)
    plain = "\n".join(l for l in out if "see that recipe" not in l and "light coconut milk" not in l.lower()
                      and "low fodmap milk" not in l.lower())
    vegan = bool(re.search(r"tofu|tempeh|vegan|vegetable|mushroom", title, re.I)) and not NOT_VEGAN.search(plain + " " + title)
    return dict(handle=rec["url"].rsplit("/", 1)[-1], sauce=sauce, subfolder=SUBFOLDER[sauce], vegan=vegan,
                name=t2["name"], ingredients=t2["ingredients"],
                description=t2["description"], directions=directions, notes="\n".join(notes), servings=serv,
                prep=rec.get("prep"), cook=rec.get("cook"), image=rec.get("image"), url=rec["url"], type=kind,
                orig_sat=(re.search(r"saturates\s*[–-]\s*([\d.]+)", rec["nutrition"]) or [None, None])[1])


def main() -> None:
    d = json.loads(DATA.read_text(encoding="utf-8"))
    drafts = [adapt(r) for h, r in d.items() if r.get("uses") and h not in EXCLUDE]
    drafts.sort(key=lambda x: (x["sauce"], x["name"]))
    OUT_JSON.write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# Bay's Kitchen recipes: low FODMAP, low saturated fat drafts", ""]
    for x in drafts:
        md += [f"## {x['name']}  ({x['type']}, serves {x['servings']})", "", x["ingredients"], "", x["directions"], "", x["notes"], ""]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    for x in drafts:
        sat = re.search(r"Sat fat ([\d.]+)g", x["description"] or "")
        print(f"{x['sauce'][:12]:12} | {x['name'][:62]:62} | {x['type'][:10]:10} | sat {sat.group(1) if sat else '?':>4} (site {x['orig_sat']})")


if __name__ == "__main__":
    main()
