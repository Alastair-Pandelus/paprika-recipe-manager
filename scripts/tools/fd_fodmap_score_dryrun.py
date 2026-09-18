"""Score Field Doctor single-serve dry-run grams vs Monash-style green limits.

Reads scripts/tools/.fd_single_serve_dryrun/*.json and writes:
  - .fd_single_serve_dryrun/_fodmap_report.json
  - canvas update is separate (see generate step)

Not medical advice; confirm serves in the Monash FODMAP app.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / ".fd_single_serve_dryrun"
REPORT_PATH = OUT_DIR / "_fodmap_report.json"

# Common Monash-app green serves (g). Confirm in app.
GREEN = {
    "brown_rice_pasta_cooked": 150,
    "rice_cooked": 190,
    "quinoa_cooked": 155,
    "canned_tomato": 100,
    "tomato_paste": 28,
    "sundried_tomato": 8,
    "red_pepper": 43,
    "green_pepper": 75,
    "carrot": 75,
    "fennel": 48,
    "spinach": 150,
    "sweet_potato": 70,
    "courgette": 66,
    "aubergine": 75,
    "chickpeas_canned_drained": 42,
    "lentils_canned_drained": 46,
    "kidney_beans_canned": 40,
    "black_beans_canned": 40,
    "mushrooms": 47,  # button; shiitake stricter
    "olives": 30,
    "parmesan": 40,
    "coconut_milk": 60,
    "apple": 0,  # high — flag any
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def classify(name: str) -> tuple[str | None, float | None, str]:
    """Return (stack_group, green_limit_g, key) or (None, None, '')."""
    n = norm(name)
    # pasta / grains
    if "penne" in n or "pasta" in n or "noodle" in n:
        if "rice" in n or "gluten" in n or "brown" in n:
            # dry grams in scrape — estimate cooked ≈ dry × 2
            return "fructans", GREEN["brown_rice_pasta_cooked"], "brown_rice_pasta_cooked"
        return "fructans", GREEN["brown_rice_pasta_cooked"], "pasta_generic"
    if "quinoa" in n:
        return "fructans", GREEN["quinoa_cooked"], "quinoa"
    if re.search(r"\brice\b", n) and "flour" not in n and "paper" not in n:
        return "fructans", GREEN["rice_cooked"], "rice"
    # tomato family
    if "sundried" in n or "sun-dried" in n or "sun dried" in n:
        return "fructose", GREEN["sundried_tomato"], "sundried_tomato"
    if "tomato puree" in n or "tomato paste" in n or "puree" in n and "tomato" in n:
        return "fructose", GREEN["tomato_paste"], "tomato_paste"
    if "plum tomato" in n or "chopped tomato" in n or (
        "tomato" in n and ("tin" in n or "canned" in n or "passata" in n)
    ):
        return "fructose", GREEN["canned_tomato"], "canned_tomato"
    if "tomato" in n and "stock" not in n:
        return "fructose", GREEN["canned_tomato"], "tomato_other"
    # peppers
    if "green pepper" in n or "green bell" in n:
        return "fructose", GREEN["green_pepper"], "green_pepper"
    if "pepper" in n and "black" not in n and "white" not in n and "chilli" not in n:
        return "fructose", GREEN["red_pepper"], "red_pepper"
    # legumes / soy
    if "beansprout" in n or "bean sprout" in n:
        return None, 72, "beansprouts"  # typically green to ~1 cup
    if "edamame" in n:
        return "gos", 42, "edamame"
    if "tofu" in n:
        return None, 170, "firm_tofu"  # firm tofu generally green in large serves
    if "soy milk" in n or "soya milk" in n:
        return "gos", 40, "soy_milk"  # varies by brand; conservative
    if "chickpea" in n or "garbanzo" in n:
        return "gos", GREEN["chickpeas_canned_drained"], "chickpeas"
    if "lentil" in n:
        return "gos", GREEN["lentils_canned_drained"], "lentils"
    if "kidney bean" in n:
        return "gos", GREEN["kidney_beans_canned"], "kidney_beans"
    if "black bean" in n:
        return "gos", GREEN["black_beans_canned"], "black_beans"
    if "bean" in n and "green bean" not in n and "runner" not in n and "soy" not in n and "soya" not in n:
        return "gos", GREEN["kidney_beans_canned"], "beans_other"
    # veg
    if "carrot" in n:
        return None, GREEN["carrot"], "carrot"
    if "fennel" in n:
        return "fructans", GREEN["fennel"], "fennel"
    if "spinach" in n:
        return None, GREEN["spinach"], "spinach"
    if "sweet potato" in n:
        return "mannitol", GREEN["sweet_potato"], "sweet_potato"
    if "courgette" in n or "zucchini" in n:
        return None, GREEN["courgette"], "courgette"
    if "aubergine" in n or "eggplant" in n:
        return None, GREEN["aubergine"], "aubergine"
    if "mushroom" in n or "shiitake" in n:
        return "mannitol", GREEN["mushrooms"], "mushrooms"
    if "olive" in n and "oil" not in n:
        return None, GREEN["olives"], "olives"
    if "parmigiano" in n or "parmesan" in n:
        return "lactose", GREEN["parmesan"], "parmesan"
    if "coconut milk" in n:
        return "polyols", GREEN["coconut_milk"], "coconut_milk"
    return None, None, ""


def cooked_equiv(key: str, grams: float) -> float:
    """Scrape amounts for pasta/grains are ready-meal mass ≈ cooked, or dry flour pasta.

    Field Doctor labels list cooked/prepared meal composition — treat pasta/rice
    grams as cooked-equivalent unless the name is explicitly dry flour pasta.
    """
    return grams


def score_recipe(artifact: dict) -> dict:
    stacks = {
        "fructans": 0.0,
        "fructose": 0.0,
        "gos": 0.0,
        "mannitol": 0.0,
        "polyols": 0.0,
        "lactose": 0.0,
    }
    # For load % we also track against primary green budgets
    tomato_g = 0.0
    pasta_g = 0.0
    rows = []
    max_pct = 0.0
    drivers = []

    for it in artifact.get("ingredient_detail") or []:
        name = it.get("name") or ""
        grams = float(it.get("grams") or 0)
        group, limit, key = classify(name)
        amt = cooked_equiv(key, grams)
        pct = (100.0 * amt / limit) if limit else 0.0
        if key in {"canned_tomato", "tomato_paste", "sundried_tomato", "tomato_other"}:
            tomato_g += grams
        if key in {"brown_rice_pasta_cooked", "pasta_generic"}:
            pasta_g += amt
        if group and limit:
            # contribution toward group: fraction of that food's green serve
            stacks[group] += amt / limit
        if limit and pct >= 40:
            rows.append(
                {
                    "name": name,
                    "grams": round(grams, 1),
                    "green_g": limit,
                    "pct_of_green": round(pct, 0),
                    "stack_group": group or "—",
                    "key": key,
                }
            )
            max_pct = max(max_pct, pct)
            if pct >= 90:
                drivers.append(f"{name.split('[')[0].strip()} ({pct:.0f}%)")

    tomato_pct = 100.0 * tomato_g / GREEN["canned_tomato"] if tomato_g else 0.0
    pasta_pct = (
        100.0 * pasta_g / GREEN["brown_rice_pasta_cooked"] if pasta_g else 0.0
    )

    # Meal risk: any single ≥120% or tomato stack ≥110% or pasta ≥110% or two groups ≥0.9
    over_groups = sum(1 for v in stacks.values() if v >= 0.9)
    if max_pct >= 140 or tomato_pct >= 130 or pasta_pct >= 130 or over_groups >= 2:
        tone = "danger"
        verdict = "elevated"
    elif max_pct >= 90 or tomato_pct >= 100 or pasta_pct >= 100 or over_groups >= 1:
        tone = "warning"
        verdict = "borderline"
    else:
        tone = "success"
        verdict = "likely_ok"

    return {
        "uid": artifact.get("uid"),
        "name": artifact.get("name"),
        "serving_g": artifact.get("serving_g"),
        "verdict": verdict,
        "tone": tone,
        "tomato_stack_g": round(tomato_g, 1),
        "tomato_stack_pct": round(tomato_pct, 0),
        "pasta_g": round(pasta_g, 1),
        "pasta_pct": round(pasta_pct, 0),
        "stack_loads": {k: round(v, 2) for k, v in stacks.items() if v > 0.05},
        "notable": sorted(rows, key=lambda r: -r["pct_of_green"]),
        "drivers": drivers,
    }


def main() -> None:
    if not OUT_DIR.is_dir():
        print(f"Missing dry-run dir: {OUT_DIR}", file=sys.stderr)
        sys.exit(1)

    scored = []
    for path in sorted(OUT_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        art = json.loads(path.read_text(encoding="utf-8"))
        scored.append(score_recipe(art))

    scored.sort(key=lambda r: (r["tone"] != "danger", r["tone"] != "warning", r["name"] or ""))
    summary = {
        "recipe_count": len(scored),
        "elevated": sum(1 for r in scored if r["verdict"] == "elevated"),
        "borderline": sum(1 for r in scored if r["verdict"] == "borderline"),
        "likely_ok": sum(1 for r in scored if r["verdict"] == "likely_ok"),
        "recipes": scored,
        "limits_note": (
            "Monash-style green serves used as guides; confirm in Monash app. "
            "Not medical advice. Tomato products stacked as canned-equivalent."
        ),
    }
    REPORT_PATH.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"FODMAP report: {len(scored)} recipes | "
        f"elevated={summary['elevated']} borderline={summary['borderline']} "
        f"ok={summary['likely_ok']} | {REPORT_PATH}"
    )


if __name__ == "__main__":
    main()
