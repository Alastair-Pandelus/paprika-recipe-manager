"""Coconut oil swaps for the She Can't Eat What recipes (the user, 21 Oct 2026).

Coconut oil is about 85% saturated fat. Where it is melted into a mix or used for frying it
becomes light olive oil (the user's standard oil, mild so the flavour holds), same amount.
Where it sets the dish, liquid oil won't work, so:
- raspberry chocolate discs, florentines: the home-made chocolate (coconut oil, cacao,
  maple) becomes 150 g melted 70% dark chocolate, which sets the same way;
- key lime raw cheesecake: olive oil in the base, coconut oil in the filling halved (6 to
  3 tbsp) with a few more macadamias;
- raw lemon and pineapple tart: olive oil in the base, the 2 tbsp in the filling kept.
swap(r) takes a scraped recipe (ing, method lists) and returns (recipe, note or "").
"""
from __future__ import annotations

import re

OIL = "light olive oil"
CHOC = "150g dark chocolate (70% cocoa solids), chopped"
MELT_CHOC = ("Melt the dark chocolate gently in a heatproof bowl over a pan of barely simmering water "
             "(or in short bursts in the microwave), stirring until smooth")


def _sub_oil(s: str) -> str:
    if re.match(r"^\s*\d+\s*g\s+oil, such as\b", s, re.I):  # "115g oil, such as vegetable, olive oil or melted coconut oil"
        return re.sub(r"(?i)oil, such as.*$", OIL, s)
    s = re.sub(r"\b([Mm])elt (the )?(?:organic |refined )?coconut oil\b",
               lambda m: f"{'W' if m.group(1) == 'M' else 'w'}arm {m.group(2) or ''}{OIL}", s)
    s = re.sub(r"(?i)\b(?:melted |liquid )?(?:organic |refined )?coconut oil\b(?:,? melted(?: and cooled)?|,? liquid)?", OIL, s)
    s = re.sub(r"(?i)\blight olive oil and allow to melt\b", f"{OIL} and let it heat", s)
    return re.sub(r"(?i)until the light olive oil has become liquid and", "until", s)


def swap(r: dict) -> tuple[dict, str]:
    if not any(re.search(r"coconut oil", l, re.I) for l in r["ing"]):
        return r, ""
    url = r["url"]
    ing, method = list(r["ing"]), list(r["method"])
    if "raspberry-low-fodmap-chocolate-discs" in url:
        ing = [CHOC if l.startswith("120ml coconut oil") else "2 tablespoons maple syrup (for topping)" if l.startswith("60ml maple syrup")
               else l for l in ing if not l.startswith("60g cacao")]
        method[0] = MELT_CHOC + ", then pour into ten cupcake cases, splitting it evenly between them."
        method[1] = method[1].replace("Pop in the freezer for 30 minutes", "Pop in the fridge for 30 minutes")
        note = ("Changed: 150 g melted 70% dark chocolate instead of the home-made chocolate (120 ml coconut oil, 60 g cacao, "
                "60 ml maple syrup), which sets the same way with far less saturated fat.")
    elif "healthy-chocolate-low-fodmap-florentines" in url:
        ing = [CHOC if l.startswith("120ml coconut oil") else "2 tablespoons maple syrup (for the topping)" if l.startswith("120ml maple syrup")
               else l for l in ing if not l.startswith("60g raw cacao")]
        method = [method[0], MELT_CHOC + ", then stir in the vanilla extract and salt."] + method[4:]
        note = ("Changed: 150 g melted 70% dark chocolate for the base instead of the home-made chocolate (120 ml coconut oil, "
                "60 g cacao, 120 ml maple syrup), which sets the same way with far less saturated fat; 2 tbsp maple syrup kept for the topping.")
    elif "low-fodmap-key-lime-raw-cheesecake" in url:
        out = []
        for l in ing:
            if l.startswith("8 tbsp organic coconut oil"):
                out += [f"2 tbsp {OIL} (for the base)", "3 tbsp coconut oil (for the filling)"]
            elif l.startswith("1 1/2 cups raw macadamia"):
                out.append(l.replace("1 1/2 cups", "1 3/4 cups"))
            else:
                out.append(l)
        ing = out
        method = [m.replace("Add 2 tbsp coconut oil", f"Add 2 tbsp {OIL}").replace("and 6 tbsp coconut oil", "and 3 tbsp coconut oil")
                  for m in method]
        note = ("Changed: light olive oil in the base; the coconut oil in the filling halved (6 to 3 tbsp) with 1/4 cup more "
                "macadamias, as it needs some coconut oil to set.")
    elif "lemon-pineapple-low-fodmap-tart" in url:
        ing = [f"4 tsp {OIL}" if l.startswith("4 tsp coconut oil") else l for l in ing]
        method = [m.replace("maple syrup, coconut oil, sea salt and ginger", f"maple syrup, {OIL}, sea salt and ginger") for m in method]
        note = "Changed: light olive oil in the crust instead of coconut oil; the 2 tbsp in the filling kept, as it sets the tart."
    else:
        ing = [_sub_oil(l) for l in ing]
        method = [_sub_oil(m) for m in method]
        note = ("Changed: light olive oil instead of coconut oil (same amount), for far less saturated fat; "
                "a few drops of coconut extract bring back the coconut note if wanted.")
        if "vegan-coconut-green-low-fodmap-curry" in url:  # the user, 21 Oct 2026
            ing = [re.sub(r"(?i)(?<!light )coconut milk", "light coconut milk", l) for l in ing]
            method = [re.sub(r"(?i)(?<!light )coconut milk", "light coconut milk", m) for m in method]
            note += "\nChanged: light coconut milk instead of full-fat coconut milk, for far less saturated fat."
    return dict(r, ing=ing, method=method), note
