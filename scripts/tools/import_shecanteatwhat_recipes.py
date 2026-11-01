"""Import She Can't Eat What recipes (data/shecanteatwhat/recipes.json) into Paprika.

The site sits behind a Cloudflare check, so the recipes were read in the built-in browser
(each post's recipe block) and saved to the JSON file first. This script:
- formats every line to the house rules (amount first, pack weight as the amount, low
  FODMAP milk, section headers end with ":"), and lists lines it could not give an amount;
- scores FODMAPs and saturated fat per serving;
- leaves out anything ❌ for FODMAPs or 💔 for saturated fat (over 10 g a serving), and any
  recipe already in the library (same source URL or name);
- files the rest under Low Fodmap > Websites > She Can't Eat What, plus its Type folder
  (and Diet > Vegan when plant-based).
Writes docs/shecanteatwhat-import.md (the report). Dry run unless --apply.

  python scripts/tools/import_shecanteatwhat_recipes.py [--apply]
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import hashlib
import html
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

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from cleanup_library import LOCAL_DB, calc_hash, format_line  # noqa: E402
from fix_amount_first_lines import rewrite as amount_first  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from shecanteatwhat_coconut_swaps import swap as coconut_swap  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402

DATA = ROOT / "data" / "shecanteatwhat" / "recipes.json"
REPORT = ROOT / "docs" / "shecanteatwhat-import.md"
FOLDER = "She Can't Eat What"
SOURCE = "She Can't Eat What"
MILK = "unsweetened almond milk (or soy-protein soy milk, or lactose-free semi-skimmed milk)"  # favoured first; scored as almond
# site category id -> Low Fodmap > Type folder
TYPE_BY_CAT = {2: "Baking & Sweet Treats", 6: "Breakfast", 123: "Baking & Sweet Treats", 34: "Smoothies and Drinks",
               16: "Main Meals", 124: "Main Meals", 122: "Sides & Salads", 7: "Snacks & Bars", 1: "Soups"}
NOT_VEGAN = re.compile(r"\b(chicken|beef|pork|lamb|turkey|duck|sausages?|bacon|ham|chorizo|prawns?|salmon|fish|cod|haddock|"
                       r"mackerel|tuna|sardines?|crab|lobster|scallops?|mussels|squid|shellfish|seafood|meat|mince|steak|venison|"
                       r"lard|suet|ghee|kefir|whey|eggs?|cheese|mozzarella|cheddar|parmesan|feta|halloumi|ricotta|mascarpone|"
                       r"butter(?!nut|milk)|buttermilk|milk|cream|yogh?urt|honey|anchov\w*|gelatine|crème fraîche|creme fraiche)\b", re.I)
AMT = re.compile(r"^\s*(\d|[½⅓⅔¼¾⅛])")


SERVE_AMOUNT = [  # (pattern, amount) for lines with no amount that are only to serve or to taste
    (r"(oil|butter|margarine|ghee).*(greas|for the (tin|pan|dish))", "1 tsp"), (r"oil|butter|ghee", "1 tbsp"), (r"yog(h)?urt|cream|crème|creme|jam|preserve|curd|honey|syrup", "2 tbsp"),
    (r"sugar|icing|cinnamon|cumin|coriander powder|spice|paprika|seeds", "1 tsp"), (r"nuts|hazelnuts|pecans|almonds|coconut", "1 tbsp"),
    (r"herbs?|mint|coriander|parsley|basil|dill|chives|sage|rosemary|thyme|leaves", "1 small handful"),
    (r"parmesan|feta|cheese", "15 g"), (r"water", "250 ml"), (r"lemon|lime", "1/2"), (r"milk", "100 ml"),
    (r"vinegar|sauce|sriracha|sprinkles|decorations?", "1 tsp"),
]
WORD_NUM = [(r"\bhalf an?\b", "1/2"), (r"\bhalf\b", "1/2"), (r"\ba couple of\b", "2"), (r"\bone\b", "1"), (r"\btwo\b", "2"),
            (r"\bthree\b", "3"), (r"\bfour\b", "4"), (r"\ba few\b", "3")]


def amountless(l: str) -> str | None:
    """Give a line with no amount a sensible one, use in brackets; None when unsure."""
    s = l.strip().rstrip(",.")
    s = re.sub(r"\(([^()]*)\(([^()]*)\)\)", r"(\1, \2)", s)  # "(to serve (optional))" -> "(to serve, optional)"
    for pat, rep in WORD_NUM:
        s2 = re.sub(r"^((?:the\s+)?(?:grated\s+)?(?:zest|juice|zest and juice|juice and zest|green part) of\s+)?" + pat,
                    lambda m: (m.group(1) or "") + rep, s, count=1, flags=re.I)
        if s2 != s:
            fixed = amount_first(s2) or s2
            if AMT.match(fixed):
                return fixed
    m = re.match(r"(?i)^(?:a|an)\s+(?!few\b|little\b|bit\b|couple\b|pinch\b|splash\b|handful\b|dash\b|drizzle\b|knob\b|squeeze\b|"
                 r"cube\b|glug\b|small\b|large\b|big\b|generous\b|good\b|sprinkle\b|shake\b)(.+)$", s)
    if m:
        return f"1 {m.group(1)}"
    s = re.sub(r"(?i)^(an?\s+)?(large|small|big|generous|good)\s+(pinch|handful|splash)\b", lambda m: f"{m.group(1) or ''}{m.group(3)}", s)
    garnish = re.match(r"(?i)^garnish(?:\s+with)?\s*[:–-]?\s*(.+)$", s)
    if garnish:
        s = garnish.group(1).strip() + " (to garnish)"
    s = re.sub(r"(?i)^(?:some|a little|a bit of|a little bit of)\s+", "", s)
    m = re.match(r"(?i)^(.+?),\s*only the green (?:part|tops?)$", s)
    if m:
        return f"1 {m.group(1)} (green part only)"
    low = s.lower()
    if re.search(r"\b(salt|pepper)\b", low) and not re.search(r"\d|pepper(s|corns)\b|red pepper|green pepper|bell|jalape", low) and len(s) < 70:
        core = re.sub(r",?\s*(more )?to (taste|season|serve)|\(.*?\)|^(an?\s+)?(big|generous|good)?\s*(few )?(pinch(es)?|grinds?|shakes?|dash) of ",
                      "", s, flags=re.I).strip(" ,")
        return "1 pinch " + core[0].lower() + core[1:] + " (to taste)"
    m = re.match(r"^(?:an?\s+)?(?:good\s+|big\s+|generous\s+)?(pinch|splash|drizzle|dash|handful|knob|squeeze|sprinkle(?=\s+of)|glug|shake|cube)"
                 r"(?:es|s)?\s+(?:of\s+)?(.+)$", s, re.I)
    if m:
        unit = {"glug": "tbsp", "shake": "pinch"}.get(m.group(1).lower(), m.group(1).lower())
        return f"1 {unit} {m.group(2)}"
    m = re.match(r"^(?:an?\s+)?(small|medium|large|big|generous|thumb-sized)\s+(bunch|handful|chunk|piece|knob|pinch)\s+(?:of\s+)?(.+?)(?:\s+(for .+|to .+))?$", s, re.I)
    if m:
        use = f" ({m.group(4)})" if m.group(4) else ""
        return f"1 {m.group(1).lower()} {m.group(2).lower()} {m.group(3)}{use}"
    uses = (r"to serve|for serving|to garnish|for garnish|to decorate|for decoration|for greasing|for the tin|for the pan|for dusting|"
            r"for frying|for cooking|for drizzling|for sprinkling|to top|for topping|to finish|to taste|as needed|optional")
    use = re.search(rf"\(?\b({uses}|of (?:your )?choice)\b\)?", s, re.I)
    core = re.sub(rf",?\s*[-–]?\s*\(?\b({uses})\b\)?", "", s, flags=re.I).strip(" ,-–")
    if core and (use or len(s) < 30):
        amt = next((a for pat, a in SERVE_AMOUNT if re.search(pat, core, re.I)), "1 portion")
        tag = f" ({use.group(1).lower()})" if use else ""
        return f"{amt} {core[0].lower() + core[1:]}{tag}"
    return None


MILK_WORDS = (r"(?:(?:whole|full[- ]fat|semi[- ]skimmed|skimmed|cow'?s|lactose[- ]free|lactose-|dairy[- ]free|plant[- ]based|"
              r"oat|rice|soy|soya|almond|unsweetened|other|fodmap[- ]friendly|any|or|,)\s*)*milk\b")


def milk(l: str) -> str:
    """Plain milk, or a choice of milks, becomes the standard low FODMAP milk line (rule 8a).
    Almond milk on its own is already fine; coconut milk, buttermilk and the like are left alone."""
    if MILK in l or re.search(r"coconut|butter ?milk|condensed|evaporated|powder|nut milk|hazelnut milk", l, re.I):
        return l
    m = re.match(rf"^(\s*[\d½¼¾⅓⅔./\s-]+\s*(?:ml|millilitres|l|tbsp|tablespoons?|tsp|teaspoons?|cups?|g)?\s+)(?:of\s+)?"
                 rf"({MILK_WORDS}(?:\s+of (?:your )?choice)?(?:\s+or\s+{MILK_WORDS})?(?:\s+of (?:your )?choice)?)(.*)$", l, re.I)
    if not m or re.fullmatch(r"(unsweetened\s+)?almond milk", m.group(2).strip(), re.I) and not re.match(
            r"^\s*,?\s*or\s+milk of", m.group(3), re.I):
        return l
    rest = re.sub(r"^\s*\((?:I use|I used) [^)]*\)", "", m.group(3))
    rest = re.sub(r"^\s*\(?,?\s*(?:or\s+)?(?:almond milk|(?:milk )?of (?:your )?choice)\)?", "", rest, flags=re.I)
    return f"{m.group(1).rstrip()} {MILK}{rest}"


def servings(s: str) -> tuple[str, str]:
    m = re.search(r"serves\s*(\d+)\s*(?:[-–]|to)\s*(\d+)", s, re.I)
    if m:
        return m.group(1), f"Serves {m.group(1)} (the website says {m.group(1)}-{m.group(2)}; the lower number is used)."
    if re.search(r"\d\s*(hours?|hrs?|minutes|mins)\b", s, re.I):
        return "", ""  # a cooking time in the servings box
    m = (re.search(r"serves\s*(?:around|about|approx\.?)?\s*(\d+)", s, re.I)
         or re.search(r"makes\s*(?:around|about|approx\.?)?\s*(\d+)", s, re.I))
    if not m or m.group(1) == "1" and re.search(r"makes\s*1\s+\w*\s*(jar|tart|loaf|cake|batch|tin|tray)", s, re.I):
        return "", ""  # "Makes 1 large jar" is a batch, not a serving
    return m.group(1), ""


def clean_notes(t: str) -> str:
    keep = []
    for l in t.split("\n"):
        l = l.strip()
        if not l or re.search(r"photograph|tag me|#shecanteatwhat|instagram|higher fodmap|^recipe notes:?$|^fodmap recipe notes:?$|"
                              r"love seeing|affiliate|cookbook|e-book", l, re.I):
            continue
        keep.append(l)
    return "\n".join(keep)


def prepare(r: dict) -> dict:
    lines, changed, review, prose = [], [], [], []
    r, swap_note = coconut_swap(r)
    for raw in r["ing"]:
        l = html.unescape(raw).replace("\xa0", " ").strip()
        if not l:
            continue
        m = re.match(r"^((?:for|to) [^:]{2,50}):\s*(\S.*)$", l, re.I)
        if m and not l.startswith("## "):  # "For the topping: 1 1/2 cups oats" -> header + line
            lines.append(m.group(1).strip() + ":")
            l = m.group(2)
        if l.startswith("## ") or (not AMT.match(l) and re.fullmatch(r"((for|to) (the )?[\w &'-]{2,50}|[A-Z][\w &'-]{2,50}),?( if you wish)?:?", l, re.I)
                                   and (l.endswith(":") or re.match(r"(for|to) ", l, re.I)) and not re.search(r"salt|pepper", l, re.I)):
            lines.append(l.lstrip("# ").rstrip(":") + ":")
            continue
        if re.match(r"(serve with|double the weight|small amount of)", l, re.I):
            prose.append(l)  # prose, not an ingredient: to the notes
            continue
        new = format_line(l)
        new = amount_first(new) or new
        new = re.sub(r"^(\d+)\s*x\s*(\d+\s*(?:g|ml)\b)\s*(?:pack|packet|tin|can|jar|bag|tub)?s?\s*(?:of\s+)?",
                     lambda m: f"{int(m.group(1)) * int(re.match(r'\d+', m.group(2)).group())} {re.sub(r'[\d\s]', '', m.group(2))} ", new, flags=re.I)
        new = re.sub(r"^(\d+)\s*\((low FODMAP[^)]*)\)\s*(.+)$", r"\1 \3 (\2)", new, flags=re.I)
        if not AMT.match(new):
            fixed = amountless(new)
            if fixed:
                new = fixed
            else:
                review.append(new)
        m2 = milk(new)
        if m2 != new:
            changed.append(f"{new} -> {m2}")
            new = m2
        lines.append(new)
    serv, serv_note = servings(r.get("serves", ""))
    steps = [html.unescape(s).replace("\xa0", " ").strip() for s in r["method"] if s.strip()]
    directions = "\n\n".join(f"{i}. {s.lstrip('# ')}" for i, s in enumerate(steps, 1))
    notes = [f"From She Can't Eat What ({r['url']}), a low FODMAP site that follows the Monash app. For personal use."]
    site = clean_notes(r.get("notes") or "")
    if site:
        notes.append("Website notes: " + site.replace("\n", " "))
    if serv_note:
        notes.append(serv_note)
    if prose:
        notes.append("From the ingredient list: " + "; ".join(prose) + ".")
    if changed:
        notes.append("Changed: milk written as " + MILK + ".")
    if swap_note:
        notes.append(swap_note)
    title = re.sub(r"\s+", " ", html.unescape(r["title"])).strip()
    if title.upper() == title:  # the older posts shout their titles
        title = re.sub(r"\bfodmap\b", "FODMAP", title.lower())
    title = title[0].upper() + title[1:]
    ing = "\n".join(lines)
    t = f.transform_recipe(title, ing, "", "\n".join(notes), servings=serv or "1")
    t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], "\n".join(notes), servings=serv or "1")
    fod, chol, _ = c.split_title_markers(t2["name"])
    sat = re.search(r"Sat fat ([\d.]+)\s*g", t2["description"] or "")
    cat = next((TYPE_BY_CAT[x] for x in r["cats"] if x in TYPE_BY_CAT), "Main Meals")
    if re.search(r"popsicle|ice cream|ice lolly|ice lollies|sorbet", title, re.I):
        cat = "Ice Cream and Frozen Desserts"
    elif re.search(r"\bsoup\b|\bstew\b", title, re.I):
        cat = "Soups"
    elif cat == "Soups":
        cat = "Main Meals"
    elif re.search(r"(\bdip|sauce|pesto|dressing|hummus|salsa|jam|curd|spread|chutney)\)?$", title, re.I):
        cat = "Oils, Sauces, Dips and Butters"
    elif re.search(r"\bsalad\b", title, re.I) and cat == "Main Meals":
        cat = "Sides & Salads"
    elif re.search(r"\bbars?\b|flapjacks?", title, re.I) and cat in ("Baking & Sweet Treats", "Breakfast"):
        cat = "Snacks & Bars"
    plain = "\n".join(l for l in lines if MILK not in l)
    vegan = bool(re.search(r"vegan", title + " " + (r.get("intro") or ""), re.I)) or not NOT_VEGAN.search(plain + " " + title)
    vegan = vegan and not NOT_VEGAN.search(plain.replace("dairy-free", "").replace("vegan butter", "").replace("nut butter", "")
                                           .replace("peanut butter", "").replace("almond butter", "").replace("coconut milk", "")
                                           .replace("coconut cream", "").replace("almond milk", "").replace("oat milk", "").replace("cocoa butter", ""))
    prep = (r.get("time") or "").replace("minutes", "mins").replace("minute", "min").replace("hours", "hrs").replace("hour", "hr")
    return dict(url=r["url"], title=title, name=t2["name"], fod=fod, chol=chol, sat=float(sat.group(1)) if sat else None,
                ingredients=t2["ingredients"], description=t2["description"], directions=directions, notes="\n".join(notes),
                servings=serv, type=cat, vegan=vegan, img=r.get("img"), total=prep, review=review, intro=r.get("intro") or "")


def jpeg(url: str) -> bytes | None:
    if not url:
        return None
    try:
        b = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        if b.status_code != 200 or len(b.content) < 500:
            return None
        im = Image.open(io.BytesIO(b.content)).convert("RGB")
        im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        buf = io.BytesIO(); im.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


async def main() -> None:
    apply = "--apply" in sys.argv
    data = json.loads(DATA.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    have_names = {strip_marker(n).lower() for (n,) in con.execute("select name from recipes where coalesce(in_trash,0)=0")}
    have_urls = {(u or "").rstrip("/").lower() for (u,) in con.execute("select source_url from recipes where coalesce(in_trash,0)=0")}
    drafts = [prepare(r) for r in data]
    keep, out_fod, out_fat, dup, no_serv = [], [], [], [], []
    for d in drafts:
        if d["url"].rstrip("/").lower() in have_urls or d["title"].lower() in have_names:
            dup.append(d)
        elif not d["servings"]:
            no_serv.append(d)
        elif d["fod"] == "❌":
            out_fod.append(d)
        elif d["chol"] == "💔":
            out_fat.append(d)
        else:
            keep.append(d)
    md = [f"# She Can't Eat What import ({datetime.date.today().isoformat()})", "",
          f"Read {len(data)} recipes. To import: {len(keep)}. Left out: {len(out_fat)} too fatty (💔, over 10 g saturated fat a serving), "
          f"{len(out_fod)} ❌ for FODMAPs, {len(no_serv)} with no servings, {len(dup)} already in the library.", ""]
    md += ["## Left out: too fatty (💔)", ""] + [f"- {d['name']} ({d['sat']} g a serving) {d['url']}" for d in out_fat] + [""]
    md += ["## Left out: ❌ FODMAPs", ""] + [f"- {d['name']} {d['url']}" for d in out_fod] + [""]
    md += ["## Left out: no servings on the site", ""] + [f"- {d['title']} {d['url']}" for d in no_serv] + [""]
    md += ["## Already in the library", ""] + [f"- {d['title']}" for d in dup] + [""]
    md += ["## To import", ""]
    for d in sorted(keep, key=lambda x: (x["type"], x["name"])):
        md += [f"### {d['name']}  ({d['type']}{', vegan' if d['vegan'] else ''}; serves {d['servings']}; sat fat {d['sat']} g a serving)", "",
               d["ingredients"], ""] + ([f"Lines with no amount (left as they are): {'; '.join(d['review'])}", ""] if d["review"] else [])
    REPORT.write_text("\n".join(md), encoding="utf-8")
    print(md[2])
    print("lines with no amount:", sum(len(d["review"]) for d in keep), "in", sum(1 for d in keep if d["review"]), "recipes")
    print("types:", {t: sum(1 for d in keep if d["type"] == t) for t in sorted({d["type"] for d in keep})}, "vegan", sum(d["vegan"] for d in keep))
    if not apply:
        return
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    log = []
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=h)
        cats = [x for x in b["result"] if not x.get("deleted")]

        def child(parent, name):
            return next((x for x in cats if x["name"] == name and x.get("parent_uid") == parent["uid"]), None)
        low = next(x for x in cats if x["name"] == "Low Fodmap" and not x.get("parent_uid"))
        web, typ, vegan = child(low, "Websites"), child(low, "Type"), child(child(low, "Diet"), "Vegan")
        folder = child(web, FOLDER)

        async def post(url, obj, photo=None, photo_name=None):
            import gzip
            form = aiohttp.FormData()
            form.add_field("data", gzip.compress(json.dumps(obj, separators=(",", ":")).encode()),
                           content_type="application/octet-stream", filename="data")
            if photo:
                form.add_field("photo_upload", photo, filename=photo_name, content_type="image/jpeg")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as r:
                assert '"result":true' in (await r.text()).replace(" ", ""), url
        if not folder:
            folder = {"uid": str(uuid.uuid4()).upper(), "name": FOLDER, "parent_uid": web["uid"], "order_flag": 0, "deleted": False}
            await post(f"{PAPRIKA_API}/v2/sync/categories/", [folder])
            print("created folder", FOLDER)
        for d in keep:
            img = jpeg(d["img"])
            uid = str(uuid.uuid4()).upper(); photo = f"{str(uuid.uuid4()).upper()}.jpg" if img else None
            rec = {
                "uid": uid, "name": d["name"], "ingredients": d["ingredients"], "directions": d["directions"],
                "description": (d["intro"] + "\n\n" if d["intro"] else "") + (d["description"] or ""), "notes": d["notes"],
                "nutritional_info": "", "servings": d["servings"], "difficulty": "", "prep_time": "", "cook_time": "",
                "total_time": d["total"], "source": SOURCE, "source_url": d["url"], "image_url": None, "photo": photo,
                "photo_large": photo, "photo_hash": hashlib.sha256(img).hexdigest().upper() if img else None, "photo_url": None,
                "categories": [folder["uid"], child(typ, d["type"])["uid"]] + ([vegan["uid"]] if d["vegan"] and vegan else []),
                "rating": 0, "in_trash": False, "is_pinned": False, "on_favorites": False, "on_grocery_list": False,
                "scale": None, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            rec["hash"] = calc_hash(rec)
            await post(f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", rec, img, photo)
            st, b = await api_json(s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=h)
            assert b["result"]["ingredients"] == rec["ingredients"], d["name"]
            log.append([uid, d["name"], datetime.date.today().isoformat(), "6", "; ".join(d["review"]), "new: She Can't Eat What import"])
            print("OK", d["name"], "photo" if img else "NO PHOTO", flush=True)
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as r:
            print("notify", r.status)
    with open(ROOT / "docs" / "recipe-audit-log.tsv", "a", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter="\t").writerows(log)
    print("created", len(log))


if __name__ == "__main__":
    asyncio.run(main())
