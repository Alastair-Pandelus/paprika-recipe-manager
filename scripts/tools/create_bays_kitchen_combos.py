"""Create the three Bay's Kitchen cook-together recipes (the user, 27 Oct 2026): the Thai trio,
the Indian trio and the tomato pair from docs/bays-kitchen-batch-plan.md. Each lists every
sauce's ingredients under its own header (so the shopping list and scaling still work),
opens with the shared prep, then each sauce's method in cook order, then one freeze step.
The photo is a composite of the sauces' own photos. Filed in Low Fodmap > Websites >
Bay's Kitchen and Type > Oils, Sauces, Dips and Butters. The single-sauce recipes are left
as they are. Updates a combo that already exists (keeping its photo). Dry run unless --apply.

  python scripts/tools/create_bays_kitchen_combos.py [--apply]
"""
from __future__ import annotations

import asyncio
import datetime
import gzip
import hashlib
import io
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path

import aiohttp
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import bays_kitchen_batch_plan as bp  # noqa: E402
from bays_kitchen_sauces import PORTION, SAUCES  # noqa: E402
from cleanup_library import LOCAL_DB, TAG_RE, calc_hash  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from rescore_small_units import rescore  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402
from bays_kitchen_bowls import BOWLS  # noqa: E402

COMBOS = [
    ("Bay's Kitchen Thai Trio – Thai Green, Thai Red & Massaman Sauces", "Thai trio", ("Thai Green", "Thai Red", "Massaman")),
    ("Bay's Kitchen Indian Trio – Korma, Tikka Masala & Katsu Sauces", "Indian trio", ("Korma", "Tikka", "Katsu")),
    ("Bay's Kitchen Tomato Pair – Tomato & Basil and Spicy Arrabbiata Sauces", "tomato pair", ("Tom & Basil", "Arrabbiata")),
]
IMAGES = ROOT / "data" / "bays_kitchen" / "combo_images"


def composite(photos: list[bytes]) -> bytes:
    """Square 1200 px image: the sauces' photos side by side as vertical strips."""
    size, n = 1200, len(photos)
    out = Image.new("RGB", (size, size), "white")
    w = size // n
    for i, raw in enumerate(photos):
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        # trim the white margin around the jar, then fit the whole jar in its strip
        bg = Image.new("RGB", im.size, (255, 255, 255))
        from PIL import ImageChops
        box = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
        if box:
            im = im.crop(box)
        scale = min((w - 20) / im.width, (size - 40) / im.height)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
        out.paste(im, (i * w + (w - im.width) // 2, (size - im.height) // 2))
        if i:
            for y in range(size):  # thin white divider
                for x in range(i * w - 3, i * w + 3):
                    out.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


QUAL = {"ginger": "peeled", "lemongrass": "tender inner part", "coriander_fresh": "stalks and leaves",
        "red_chilli": "deseeded for less heat", "green_chilli": "deseeded for less heat",
        "cardamom": "lightly crushed", "pineapple": "finely diced"}
COUNT_UNITS = ("pods", "pod", "leaves", "leaf", "cloves", "clove")


def amt(q, u) -> str:
    """An amount for a line: counts drop their unit word, which the name already says ("3 green cardamom pods")."""
    out = bp.fmt(q, u)
    return out.rsplit(" ", 1)[0] if out.rsplit(" ", 1)[-1] in COUNT_UNITS else out
PREP = {  # shared-prep task for an ingredient every sauce in the group uses
    "ginger": "Peel and finely grate all the ginger.",
    "lemongrass": "Trim all the lemongrass to the tender inner part and chop it finely.",
    "coriander_fresh": "Wash all the coriander; chop the stalks and the leaves separately.",
    "lime": "Squeeze all the lime juice.",
    "lemon": "Squeeze all the lemon juice.",
    "chives": "Chop all the chives.",
    "red_pepper": "Dice or slice all the red pepper.",
    "green_pepper": "Dice or slice all the green pepper.",
    "coconut": "Open the light coconut milk and stir it well.",
    "tomatoes": "Open the tinned chopped tomatoes.",
}
MENTION = {  # how each ingredient is named in the methods, for ordering by first use
    "coconut": r"coconut milk", "tapioca": r"tapioca|cornflour", "sugar": r"sugar", "maple": r"maple",
    "oil": r"olive oil|\boil\b", "tamari": r"tamari", "lemon": r"lemon juice", "lime": r"lime juice",
    "ginger": r"ginger", "lemongrass": r"lemongrass", "coriander_fresh": r"coriander (?:stalks|leaves)|fresh coriander|and coriander\b(?! seeds)",
    "coriander_seed": r"coriander(?: and \w+)? seeds|ground seeds", "cumin": r"cumin|ground seeds",
    "fenugreek": r"fenugreek", "fennel": r"fennel", "mustard": r"mustard", "turmeric": r"turmeric",
    "paprika": r"paprika", "cayenne": r"cayenne", "chilli_powder": r"chilli powder", "chilli_flakes": r"chilli flakes|\bchilli\b",
    "cardamom": r"cardamom", "cloves": r"cloves", "cinnamon": r"cinnamon", "oregano": r"oregano",
    "tomatoes": r"tomatoes", "tomato_puree": r"tomato purée", "sundried": r"sundried", "balsamic": r"balsamic",
    "basil": r"basil", "chives": r"chives", "green_chilli": r"green chilli", "red_chilli": r"red chillies",
    "green_pepper": r"peppers|green pepper", "red_pepper": r"peppers|red pepper", "lime_leaf": r"lime lea",
    "bamboo": r"bamboo", "salt": r"\bsalt\b", "pepper": r"black pepper", "water": r"\bwater\b",
    "pineapple": r"pineapple\b(?! juice)", "pineapple_juice": r"pineapple juice", "cider_vinegar": r"vinegar",
    "rice_vinegar": r"vinegar",
}
SEEDS = ("cumin", "coriander_seed", "fenugreek", "fennel", "mustard")


def sauce_title(r: dict) -> str:
    return r["name"].replace("Bay's Kitchen ", "")


def total(parts: list[tuple]) -> str:
    """Sum one ingredient across sauces; a pinch beside spoon amounts is noted, not added."""
    from fractions import Fraction
    main = [(q, u) for q, u in parts if u != "pinch"]
    pinches = sum(1 for _, u in parts if u == "pinch")
    if not main:
        return bp.fmt(Fraction(pinches), "pinch")
    units = {u for _, u in main}
    assert len(units) == 1, parts
    out = amt(sum((q for q, _ in main), Fraction(0)), units.pop())
    if pinches:
        out += " (plus a pinch)" if pinches == 1 else f" (plus {pinches} pinches)"
    return out


def short_bowl(name: str) -> str:
    """"Katsu spice" -> "spice bowl", "Korma oil" -> "oil": the sauce name is implied in its own section."""
    word = name.split(" ", 1)[1] if " " in name else name
    if word == "water":
        return "jug"  # "Jug: its water", "a splash from the jug"
    return word if word in ("oil", "ginger", "thickener") else f"{word} bowl"


def build(order: list[str], recs: dict) -> tuple[list[str], str]:
    disp = {k: d for _, k, d, _ in bp.CANON}
    korder = [k for _, k, _, _ in bp.CANON]
    items, optional_in = {}, {}
    for sh in order:
        items[sh] = {}
        for l in recs[sh][2]["ingredients"].split("\n"):
            if l.strip():
                k, q, u, _ = bp.parse(TAG_RE.sub("", l).strip())
                items[sh][k] = (q, u)
                if "(optional" in l.lower():
                    optional_in.setdefault(k, set()).add(sh)
    keys = [k for k in korder if any(k in items[sh] for sh in order)]
    # optional in the combined list only when every sauce that uses it has it as optional
    optional = {k for k in keys if optional_in.get(k, set()) == {sh for sh in order if k in items[sh]}}

    def opt_note(k: str) -> str:
        return "optional, or simmer 10 minutes longer" if k == "tapioca" else "optional"

    def pct(k: str, sh: str) -> int:
        from fractions import Fraction
        size = {o: (items[o][k][0] / 16 if items[o][k][1] == "pinch" else items[o][k][0]) for o in order if k in items[o]}
        tot = sum(size.values(), Fraction(0))
        raw = {o: size[o] * 100 / tot for o in size}
        out = {o: int(raw[o]) for o in raw}
        for o in sorted(raw, key=lambda o: raw[o] - out[o], reverse=True)[: 100 - sum(out.values())]:
            out[o] += 1
        return out[sh]

    shared = [k for k in keys if all(k in items[sh] for sh in order)]  # water, salt and pepper too: one total, split by %
    # order of use: what the shared prep handles (its tasks, then the seeds), the rest of the shared
    # ingredients by first mention in the methods, then each sauce's own ingredients as its method uses them
    methods = {sh: [re.sub(r"^\d+\.\s*", "", p).strip() for p in recs[sh][2]["directions"].split("\n\n") if p.strip()]
               for sh in order}

    def first_use(k: str, text: str) -> int:
        text = re.sub(r"\([^)]*\)", "", text)  # asides ("oregano doesn't need frying") are not uses
        m = re.search(MENTION.get(k, re.escape(disp[k].split(" (")[0].lower().split()[-1])), text, re.I)
        return m.start() if m else 10 ** 6

    all_text = " ".join(" ".join(methods[sh]) for sh in order)
    tasks = [k for k in shared if k in PREP]
    seeds_first = [k for k in SEEDS if k in shared]
    rest = sorted((k for k in shared if k not in tasks and k not in seeds_first), key=lambda k: first_use(k, all_text))
    shared = tasks + seeds_first + rest
    use_order = list(shared)
    for sh in order:
        text = " ".join(methods[sh])
        mine = sorted((k for k in keys if k in items[sh] and k not in use_order), key=lambda k: first_use(k, text))
        use_order += mine
    # 2. shared prep: only what every sauce uses (tasks, no quantities), the seeds toasted once
    prep = [PREP[k] for k in shared if k in PREP]
    seeds = [k for k in SEEDS if k in shared]
    if seeds:
        prep.append("Dry-toast all the " + " and ".join(disp[k].lower() for k in seeds)
                    + " together in a dry pan for 1-2 minutes until fragrant, then grind.")
    own_seeds = [(sh, [k for k in SEEDS if k in items[sh] and k not in shared]) for sh in order]
    own_seeds = [(sh, ks) for sh, ks in own_seeds if ks]
    if own_seeds:
        prep.append("Toast and grind separately, the same way: " + "; ".join(
            f"the {sh} " + " and ".join(disp[k].lower() for k in ks) for sh, ks in own_seeds) + "."
            + (" Fenugreek turns bitter if it goes past golden." if any("fenugreek" in ks for _, ks in own_seeds) else ""))
    steps = ["**Shared prep, for all the sauces:**\n" + "\n".join(f"- {p}" for p in prep)]

    # 3. fill each sauce's named bowls once; the methods then use the bowl names
    def portion(k: str, sh: str) -> str:
        base, _, part = k.partition(":")
        name = disp[base].split(" (")[0].lower()
        if base in SEEDS:
            name = "ground " + name
        txt = f"{pct(base, sh)}% of the {name}" if base in shared else f"its {name}"  # "its": from its own list
        if part:  # "the stalks from 57% of the fresh coriander"
            txt = f"the {part} from " + (txt if base in shared else f"its {name}")
        return txt + (" (optional)" if base in optional else "")

    fill = []
    for sh in order:
        bowls, _ = BOWLS[sh]
        covered = {k.partition(":")[0] for _, ks in bowls for k in ks}
        missing = set(items[sh]) - covered
        assert not missing, (sh, missing)
        fill.append(f"**{sh}:**\n" + "\n".join(f"- {short_bowl(name).capitalize()}: " + ", ".join(portion(k, sh) for k in ks)
                                            for name, ks in bowls))
    steps.append("**Fill the bowls.** Weigh each sauce's share of the shared prep (the percentages) and its own ingredients "
                 "(\"its …\", from its section of the ingredient list) into its bowls:\n\n" + "\n\n".join(fill))
    for sh in order:
        handle, r, x = recs[sh]
        _, method = BOWLS[sh]
        # within a sauce's own section the sauce name is implied: "**Katsu oil**" -> "**oil**"
        method = [re.sub(r"\*\*([^*]+)\*\*", lambda m: f"**{short_bowl(m.group(1))}**", st) for st in method]
        method = [st.replace("the rest of the **jug**", "the rest of the water from the **jug**")
                    .replace("Add the **jug**", "Add the water from the **jug**") for st in method]
        steps.append(f"**{sauce_title(r)}**\nSingle recipe: [recipe:{x['name']}]\n" + "\n".join(f"- {m}" for m in method))
    # 1. the shopping list in sections: what every sauce shares (totals), then each sauce's own
    # ingredients (its own amounts), each ingredient listed once, amount first, in order of use
    lines = []

    def line(k: str, parts: list[tuple]) -> str:
        q = QUAL.get(k, "")
        name = disp[k][0].lower() + disp[k][1:]
        tot = total(parts)
        about = tot.startswith("about ")  # rule 1: the amount comes first; "about" goes in brackets at the end
        tot = tot.removeprefix("about ")
        plus = ""
        if " (plus " in tot:  # "1/4 tsp (plus a pinch)": the extra goes after the name
            tot, plus = tot.split(" (", 1)
            plus = plus.rstrip(")")
        qual = ", ".join(x for x in (q, plus, "about" if about else "", opt_note(k) if k in optional else "") if x)
        return f"{tot} {name}" + (f" ({qual})" if qual else "")

    lines.append("Shared (all the sauces):")
    lines += [line(k, [items[sh][k] for sh in order]) for k in shared]
    for sh in order:
        text = " ".join(methods[sh])
        mine = sorted((k for k in keys if k in items[sh] and k not in shared), key=lambda k: first_use(k, text))
        if mine:
            lines.append(f"{sauce_title(recs[sh][1])}:")
            lines += [line(k, [items[sh][k]]) for k in mine]
    steps.append("Cool, then split each sauce into single portions and freeze, labelled with the sauce and date: "
                 + ", ".join(f"{sh} {PORTION[recs[sh][0]]} g" for sh in order) + ".")
    return lines, "\n\n".join(f"{i}. {st}" for i, st in enumerate(steps, 1))


async def main() -> None:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    have = {strip_marker(n).lower() for (n,) in con.execute("select name from recipes where coalesce(in_trash,0)=0")}
    live = {strip_marker(n): u for u, n in con.execute("select uid, name from recipes where coalesce(in_trash,0)=0")}
    short_of = {bp.SHORT[r["name"].replace("Bay's Kitchen ", "").replace(" Sauce", "")]: (h, r) for h, r in SAUCES.items()}
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    IMAGES.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=h)
        cats = [x for x in b["result"] if not x.get("deleted")]

        def child(parent, name):
            return next(x for x in cats if x["name"] == name and x.get("parent_uid") == parent["uid"])
        low = next(x for x in cats if x["name"] == "Low Fodmap" and not x.get("parent_uid"))
        folder = [child(child(low, "Websites"), "Bay's Kitchen")["uid"], child(child(low, "Type"), "Oils, Sauces, Dips and Butters")["uid"]]
        for name, label, group in COMBOS:
            existing = next((u for n2, u in live.items() if n2 == name), None)
            order = sorted(group, key=bp.RANK.index)
            recs = {}
            for sh in order:
                handle, r = short_of[sh]
                st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{live[r['name']]}/", headers=h)
                recs[sh] = (handle, r, b["result"])
            lines, directions = build(order, recs)
            notes = (f"Cook-together recipe for the Bay's Kitchen {label}: "
                     + ", ".join(f"[recipe:{recs[sh][2]['name']}]" for sh in order)
                     + ". The ingredient list is the combined shop (an equal number of single portions of each sauce); step 1 preps "
                     "what all the sauces share and splits it per sauce, then each sauce adds its own ingredients. "
                     "The single-sauce recipes are unchanged. See docs/bays-kitchen-batch-plan.md for the overlaps.")
            servings = str(2 * len(order))
            t2 = rescore({"name": name, "ingredients": "\n".join(lines), "description": "", "notes": notes, "servings": servings})
            photos = []
            for sh in order:
                url = recs[sh][2].get("photo_url")
                if url:
                    resp = requests.get(url, timeout=60)
                    if resp.status_code == 200:
                        photos.append(resp.content)
            jpeg = composite(photos) if len(photos) == len(order) else None
            if jpeg:
                (IMAGES / f"{label.replace(' ', '-')}.jpg").write_bytes(jpeg)
            print(f"{'UPDATE' if existing else 'NEW'} {t2['name']} | serves {servings}")
            print(t2["ingredients"])
            print(directions)
            if not apply:
                continue
            desc = (f"Cook the Bay's Kitchen {label} in one session: one shop, one prep, then each sauce in turn.\n\n"
                    "Makes these sauces, in equal numbers of single portions:\n"
                    + "\n".join(f"- [recipe:{recs[sh][2]['name']}]" for sh in order) + "\n\n"
                    + (t2["description"] or ""))
            if existing:
                st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{existing}/", headers=h)
                cur = b["result"]
                with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({k: cur.get(k) for k in ("uid", "name", "ingredients", "directions", "notes")},
                                        ensure_ascii=False) + "\n")
                rec = dict(cur, name=t2["name"], ingredients=t2["ingredients"], directions=directions, notes=notes,
                           description=desc)
                uid, jpeg = existing, None
            else:
                uid = str(uuid.uuid4()).upper(); photo = f"{str(uuid.uuid4()).upper()}.jpg" if jpeg else None
                rec = {
                    "uid": uid, "name": t2["name"], "ingredients": t2["ingredients"], "directions": directions,
                    "description": desc, "notes": notes, "nutritional_info": "", "servings": servings, "difficulty": "",
                    "prep_time": "", "cook_time": "", "total_time": "", "source": "Bay's Kitchen (home versions)",
                    "source_url": "", "image_url": None, "photo": photo, "photo_large": photo,
                    "photo_hash": hashlib.sha256(jpeg).hexdigest().upper() if jpeg else None, "photo_url": None,
                    "categories": folder, "rating": 0, "in_trash": False, "is_pinned": False, "on_favorites": False,
                    "on_grocery_list": False, "scale": None, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            rec["hash"] = calc_hash(rec)
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(rec, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            if jpeg:
                form.add_field("photo_upload", jpeg, filename=rec["photo"], content_type="image/jpeg")
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), name
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            assert b["result"]["ingredients"] == rec["ingredients"], name
            print("updated" if existing else "created", b["result"]["name"])
        if apply:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
                print("notify", resp.status)


if __name__ == "__main__":
    asyncio.run(main())
