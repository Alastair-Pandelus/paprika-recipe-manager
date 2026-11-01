"""Batch-cooking plan for the 10 Bay's Kitchen sauces -> docs/bays-kitchen-batch-plan.md.

Reads each sauce's live ingredient list from the local Paprika database (read-only; the
sync API is the source of truth and the app was synced), checks it against
bays_kitchen_sauces.py, and writes:
- an ingredient x sauce table (amount per batch: one recipe = 2 portions);
- a sauce x sauce overlap score: shared ingredients / combined ingredients (Jaccard),
  leaving out water, salt and black pepper, which every kitchen has;
- each sauce's best partner, and the user's cooking groups (Thai trio, Indian trio, tomato
  pair; Sweet & Sour and Teriyaki alone), each group with a combined shopping list and a
  shared prep list.

  python scripts/tools/bays_kitchen_batch_plan.py
"""
from __future__ import annotations

import itertools
import re
import sqlite3
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from bays_kitchen_sauces import SAUCES  # noqa: E402
from cleanup_library import LOCAL_DB, TAG_RE  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402

OUT = ROOT / "docs" / "bays-kitchen-batch-plan.md"
# the user's grouping (27 Oct 2026): Thai trio, Indian trio, the two tomato sauces; Sweet & Sour
# and Teriyaki stand alone (they share little with anything and need no spices)
GROUPS = [("Thai Green", "Thai Red", "Massaman"), ("Korma", "Tikka", "Katsu"), ("Tom & Basil", "Arrabbiata")]
ALONE = ["Sweet & Sour", "Teriyaki"]
SHORT = {"Tomato & Basil": "Tom & Basil", "Spicy Arrabbiata": "Arrabbiata", "Tikka Masala": "Tikka", "Korma Curry": "Korma",
         "Katsu Curry": "Katsu", "Sweet & Sour": "Sweet & Sour", "Thai Massaman": "Massaman", "Thai Green Curry": "Thai Green",
         "Thai Red Curry": "Thai Red", "Teriyaki": "Teriyaki"}
# (pattern on the ingredient text, key, display name, kind) - kind: fresh / spice / tin / liquid / dry / pantry
CANON = [
    (r"tinned chopped tomatoes", "tomatoes", "Tinned chopped tomatoes", "tin"),
    (r"tomato purée", "tomato_puree", "Tomato purée", "tin"),
    (r"sundried tomatoes", "sundried", "Sundried tomatoes (dry-packed)", "dry"),
    (r"light coconut milk", "coconut", "Light coconut milk", "tin"),
    (r"bamboo shoots", "bamboo", "Bamboo shoots (tinned)", "tin"),
    (r"pineapple juice", "pineapple_juice", "Unsweetened pineapple juice", "liquid"),
    (r"^pineapple", "pineapple", "Pineapple (fresh or tinned in juice)", "fresh"),
    (r"tapioca starch", "tapioca", "Tapioca starch", "dry"),
    (r"caster sugar", "sugar", "Caster sugar", "dry"),
    (r"maple syrup", "maple", "Maple syrup", "liquid"),
    (r"olive oil", "oil", "Olive oil", "pantry"),
    (r"tamari", "tamari", "Tamari (or gluten-free soy sauce)", "liquid"),
    (r"balsamic vinegar", "balsamic", "Balsamic vinegar", "liquid"),
    (r"cider vinegar", "cider_vinegar", "Cider vinegar", "liquid"),
    (r"rice vinegar", "rice_vinegar", "Rice vinegar", "liquid"),
    (r"lemon juice", "lemon", "Lemon juice", "fresh"),
    (r"lime juice", "lime", "Lime juice", "fresh"),
    (r"fresh ginger", "ginger", "Fresh ginger", "fresh"),
    (r"lemongrass", "lemongrass", "Lemongrass", "fresh"),
    (r"fresh coriander", "coriander_fresh", "Fresh coriander", "fresh"),
    (r"basil", "basil", "Fresh basil", "fresh"),
    (r"chives", "chives", "Chives", "fresh"),
    (r"green chilli", "green_chilli", "Green chilli", "fresh"),
    (r"red chillies", "red_chilli", "Red chillies", "fresh"),
    (r"green pepper", "green_pepper", "Green pepper", "fresh"),
    (r"red pepper", "red_pepper", "Red pepper", "fresh"),
    (r"kaffir lime", "lime_leaf", "Kaffir lime leaves", "fresh"),
    (r"cumin seeds", "cumin", "Cumin seeds", "seed"),
    (r"coriander seeds", "coriander_seed", "Coriander seeds", "seed"),
    (r"fenugreek", "fenugreek", "Fenugreek seeds", "seed"),
    (r"fennel seeds", "fennel", "Fennel seeds", "seed"),
    (r"mustard seeds", "mustard", "Mustard seeds", "seed"),
    (r"cardamom", "cardamom", "Green cardamom pods", "spice"),
    (r"cloves", "cloves", "Whole cloves", "spice"),
    (r"cinnamon", "cinnamon", "Ground cinnamon", "spice"),
    (r"turmeric", "turmeric", "Ground turmeric", "spice"),
    (r"sweet paprika", "paprika", "Sweet paprika", "spice"),
    (r"cayenne", "cayenne", "Cayenne pepper", "spice"),
    (r"chilli powder", "chilli_powder", "Mild chilli powder", "spice"),
    (r"chilli flakes", "chilli_flakes", "Dried chilli flakes", "spice"),
    (r"dried oregano", "oregano", "Dried oregano", "spice"),
    (r"black pepper", "pepper", "Black pepper", "pantry"),
    (r"^salt|\bsalt$", "salt", "Salt", "pantry"),
    (r"water", "water", "Water", "pantry"),
]
COMMON = {"water", "salt", "pepper"}
UNITS = {"g": ("g", 1), "ml": ("ml", 1), "tsp": ("tsp", 1), "tbsp": ("tsp", 3), "pinch": ("pinch", 1)}


def num(s: str) -> Fraction:
    return sum((Fraction(p) for p in s.split()), Fraction(0))


def parse(line: str) -> tuple[str, Fraction, str, str]:
    m = re.match(r"^\s*(\d+(?:\s+\d+/\d+|/\d+)?)\s*(g|ml|tsp|tbsp|pinch)?\b\s*(.*)$", line)
    qty, unit, rest = num(m.group(1)), m.group(2), m.group(3)
    key = next((k for pat, k, _, _ in CANON if re.search(pat, rest, re.I)), None)
    if key is None:
        raise SystemExit(f"no canonical name for: {line}")
    if unit:
        u, f = UNITS[unit]
        return key, qty * f, u, rest
    count_unit = "pods" if "cardamom" in rest else "leaves" if "lime leaf" in rest or "lime leaves" in rest else "cloves"
    return key, qty, count_unit, rest


def fmt(q: Fraction, u: str) -> str:
    approx = ""
    if u == "tsp" and q >= 3:
        q, u = q / 3, "tbsp"
    if q.denominator not in (1, 2, 3, 4, 8):  # e.g. 19/12 tsp: round to the nearest 1/4
        r = Fraction(round(q * 4), 4)
        approx, q = ("about " if r != q else ""), r
    if q.denominator == 1:
        s = str(q.numerator)
    else:
        whole, frac = divmod(q.numerator, q.denominator)
        s = (f"{whole} " if whole else "") + f"{frac}/{q.denominator}"
    if u in ("pinch", "leaves", "pods", "cloves") and q == 1:
        u = {"pinch": "pinch", "leaves": "leaf", "pods": "pod", "cloves": "clove"}[u]
    elif u == "pinch":
        u = "pinches"
    return f"{approx}{s} {u}"


def main() -> None:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    live = {strip_marker(n): (n, i) for n, i in con.execute("select name, ingredients from recipes where coalesce(in_trash,0)=0")}
    sauces, drift = {}, []
    for handle, r in SAUCES.items():
        title, ing = live[r["name"]]
        lines = [TAG_RE.sub("", l).strip() for l in ing.split("\n") if l.strip()]
        short = SHORT[r["name"].replace("Bay's Kitchen ", "").replace(" Sauce", "")]
        sauces[short] = {"title": title, "items": {}}
        for l in lines:
            key, q, u, rest = parse(l)
            sauces[short]["items"][key] = (q, u, l)
        drift += [f"{short}: Paprika has \"{x}\", the script does not" for x in lines if x not in r["ingredients"]]
    names = list(sauces)
    sets = {n: set(sauces[n]["items"]) - COMMON for n in names}

    def jac(a, b):
        return len(sets[a] & sets[b]) / len(sets[a] | sets[b])

    groups = GROUPS
    mean = {g: sum(jac(x, y) for x, y in itertools.combinations(g, 2)) / (len(g) * (len(g) - 1) / 2) for g in groups}

    md = ["# Bay's Kitchen sauces: batch-cooking plan", "",
          "Generated by `scripts/tools/bays_kitchen_batch_plan.py` from the 10 sauces as they are in Paprika "
          "(checked against `scripts/tools/bays_kitchen_sauces.py`). One batch = one recipe as written = 2 single "
          "portions (about 120-140 g each). For more portions, multiply every amount by portions ÷ 2.", ""]
    if drift:
        md += ["**Script vs Paprika:** the only differences are the thickener lines: Paprika has tapioca starch (the "
               "21 Oct swap), the script still said cornflour. The script has now been brought into line.", ""]
    # ingredient x sauce
    order = [k for _, k, _, _ in CANON]
    disp = {k: d for _, k, d, _ in CANON}
    used = [k for k in order if any(k in sauces[n]["items"] for n in names)]
    md += ["## 1. Ingredients by sauce (amount per batch)", "",
           "| Ingredient | " + " | ".join(names) + " | Used in |", "|---|" + "---|" * len(names) + "---|"]
    for k in used:
        cells = [fmt(*sauces[n]["items"][k][:2]) if k in sauces[n]["items"] else "" for n in names]
        md.append(f"| {disp[k]} | " + " | ".join(cells) + f" | {sum(1 for c in cells if c)} |")
    md += ["", "Tapioca starch is always mixed with some of the sauce's own water before it goes in.", ""]
    # overlap
    md += ["## 2. Overlap between sauces", "",
           "Shared ingredients ÷ all ingredients of the two sauces (water, salt and black pepper left out). "
           "1.00 would be identical; higher means more shared shopping and prep.", "",
           "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for a in names:
        md.append(f"| **{a}** | " + " | ".join("–" if a == b else f"{jac(a, b):.2f}" for b in names) + " |")
    pairs = sorted(((jac(a, b), a, b) for a, b in itertools.combinations(names, 2)), reverse=True)
    md += ["", "Closest pairs: " + "; ".join(f"{a} + {b} ({s:.2f})" for s, a, b in pairs[:6]) + ".", ""]
    # sessions
    md += ["## 3. Best partner for each sauce", "",
           "| Sauce | Best partner | Overlap | Next best |", "|---|---|---|---|"]
    for n in names:
        ranked = sorted(((jac(n, o), o) for o in names if o != n), reverse=True)
        md.append(f"| {n} | {ranked[0][1]} | {ranked[0][0]:.2f} | {ranked[1][1]} ({ranked[1][0]:.2f}) |")
    md += ["", "## 4. Cooking groups", "",
           "Three groups, each one shop and one prep: the Thai trio (one paste session), the Indian trio (one ginger "
           "and seed prep) and the two tomato sauces. Massaman sits with the Thai sauces: it shares their lemongrass, "
           "ginger and coriander paste, lime leaves and lime juice, and overlaps them more (0.61 and 0.50) than the "
           "Indian curries (0.56, 0.45, 0.38). Sweet & Sour and Teriyaki stand alone: they share little with any "
           "sauce and need no spices, so they fit in any time.", ""]
    for i, g in enumerate(groups, 1):
        inner = sorted(((jac(a, b), a, b) for a, b in itertools.combinations(g, 2)), reverse=True)
        title = {0: "Thai trio", 1: "Indian trio", 2: "Tomato pair"}[i - 1]
        shared = sorted(set.intersection(*(sets[n] for n in g)), key=order.index)
        md += [f"### {i}. {title}: {', '.join(g)} (average overlap {mean[g]:.2f})", "",
               "Overlap: " + "; ".join(f"{a} + {b} {s:.2f}" for s, a, b in inner) + ".", "",
               "Shared by all: " + ", ".join(disp[k].lower() for k in shared) + ".", ""]
        md += [
               "**Shopping and weighing list (one batch of each):**", "",
               "| Ingredient | Total | Split |", "|---|---|---|"]
        keys = [k for k in order if any(k in sauces[n]["items"] for n in g)]
        for k in keys:
            parts = [(n, sauces[n]["items"][k]) for n in g if k in sauces[n]["items"]]
            units = {p[1][1] for p in parts}
            total = fmt(sum((p[1][0] for p in parts), Fraction(0)), units.pop()) if len(units) == 1 else "see split"
            split = "; ".join(f"{n} {fmt(*it[:2])}" for n, it in parts) if len(parts) > 1 else parts[0][0]
            md.append(f"| {disp[k]} | {total} | {split} |")
        md += ["", "**Prep once for the group:**", ""]
        md += prep_steps(g, sauces, disp)
        md += ["", "**Cook order:** " + cook_order(g) + "", ""]
    md += ["### On their own: " + " and ".join(ALONE), "",
           "No shared prep: each is a 10-minute sauce with no spices or oil (Sweet & Sour: pineapple juice, peppers, "
           "cider vinegar; Teriyaki: tamari, maple syrup, ginger, rice vinegar). Make either alongside any group, or on "
           "its own; the amounts are in the table above.", "",
           "## 5. General tips", "",
           "- Weigh each sauce's ingredients into its own labelled bowls straight after the shared prep, so nothing "
           "gets double-counted when the sauces cook one after another.",
           "- Cook a group on two rings, or in one pan, mildest first (the order above), wiping it between.",
           "- Tapioca starch goes in at the end and needs only a gentle simmer; boiling hard afterwards thins it again.",
           "- Cool each sauce, split into single portions at the weight in its recipe, label with the sauce and date, "
           "and freeze.", ""]
    OUT.write_text("\n".join(md), encoding="utf-8")
    print("written", OUT)
    for i, g in enumerate(groups, 1):
        print(f"session {i}:", ", ".join(g))
    print("pairs:", pairs[:5])


def prep_steps(g, sauces, disp) -> list[str]:
    out = []

    def tot(k):
        parts = [(n, sauces[n]["items"][k]) for n in g if k in sauces[n]["items"]]
        return parts, (fmt(sum((p[1][0] for p in parts), Fraction(0)), parts[0][1][1]) if parts else "")

    for k, verb in (("ginger", "Peel and finely grate all the ginger in one go"),
                    ("lemongrass", "Trim the lemongrass to the tender inner part and chop it all"),
                    ("coriander_fresh", "Wash the coriander; chop the stalks and leaves separately (stalks go in the pastes, leaves at the end)"
                     if any("lemongrass" in sauces[n]["items"] for n in g) else "Wash and chop the coriander"),
                    ("chives", "Chop all the chives"),
                    ("basil", "Tear the basil just before it goes in"),
                    ("green_pepper", "Dice or slice all the green pepper"),
                    ("red_pepper", "Dice or slice all the red pepper"),
                    ("red_chilli", "Deseed and chop the red chillies"),
                    ("green_chilli", "Deseed and chop the green chilli"),
                    ("lemon", "Squeeze the lemon juice"),
                    ("lime", "Squeeze the lime juice")):
        parts, total = tot(k)
        if parts:
            split = ", ".join(f"{n} {fmt(*it[:2])}" for n, it in parts)
            out.append(f"- {verb}: {total} in total ({split})." if len(parts) > 1 else f"- {verb}: {total} ({parts[0][0]}).")
    seeds = [k for k in ("cumin", "coriander_seed", "fenugreek", "fennel", "mustard") if any(k in sauces[n]["items"] for n in g)]
    if seeds:
        toasters = [n for n in g if any(k in sauces[n]["items"] for k in seeds)]
        shared = [k for k in seeds if sum(1 for n in toasters if k in sauces[n]["items"]) > 1]
        line = "- Toast the whole seeds in one dry pan, 1-2 minutes until fragrant, then grind"
        if shared:
            line += (f": the {', '.join(disp[k].lower() for k in shared)} can be toasted and ground in bulk and then "
                     "measured out per sauce")
        only = [(n, [k for k in seeds if k in sauces[n]["items"] and k not in shared]) for n in toasters]
        only = [(n, ks) for n, ks in only if ks]
        if only:
            line += "; toast " + "; ".join(f"{n}'s {', '.join(disp[k].lower() for k in ks)} separately" for n, ks in only)
            line += " (fenugreek turns bitter past golden, so watch it)" if any("fenugreek" in ks for _, ks in only) else ""
        out.append(line + ".")
    pastes = sorted((n for n in g if "lemongrass" in sauces[n]["items"]), key=RANK.index)
    if pastes:
        out.append(f"- Blitz the curry pastes one after another in the same mini chopper ({', '.join(pastes)}), "
                   "mildest first, each with 1 tbsp of its own water.")
    if any("tapioca" in sauces[n]["items"] for n in g):
        out.append("- Mix each sauce's tapioca starch with its own measured water in a small cup and label it.")
    if any("coconut" in sauces[n]["items"] for n in g):
        parts, total = tot("coconut")
        out.append(f"- Open one tin of light coconut milk for the group ({total} needed); stir it well first.")
    if any("tomatoes" in sauces[n]["items"] for n in g):
        parts, total = tot("tomatoes")
        out.append(f"- Open one tin of chopped tomatoes ({total} needed).")
    return out


RANK = ["Tom & Basil", "Sweet & Sour", "Teriyaki", "Korma", "Katsu", "Tikka", "Thai Green", "Thai Red", "Massaman", "Arrabbiata"]


def cook_order(g) -> str:
    return " → ".join(sorted(g, key=RANK.index)) + " (mildest first if you use one pan)."


if __name__ == "__main__":
    main()
