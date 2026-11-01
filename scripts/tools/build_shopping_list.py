"""Build the week's Paprika grocery list from the Meals calendar (planning step 2).

Reads the live Meals calendar for a date range and makes a fresh shopping list:

- every "Staple - " pantry item, at its pantry quantity (a top-up reminder; the
  user ticks off what's already in stock);
- each planned recipe's ingredients, unless the meal comes from stock: a
  pantry batch item (frozen soups, bolognese, hummus), a Waitrose ready meal
  (covered by the ready-meals staple), or a line a staple covers;
- batch cooks named with --batch, scaled to --portions (default 8) once,
  however many times they appear in the plan;
- meals named with --meal (e.g. a Brick Method meal), scaled to --portions
  whether or not they are on the calendar;
- a "[recipe:Name]" line is bought as that recipe's own ingredients, scaled to
  the servings the line uses (so a Brick meal lists the Bricks' ingredients);
- salt, pepper and water are left off; the same line from several meals is
  merged; each item gets its Paprika aisle.

On --apply, the existing items on the main grocery list are backed up to
docs/groceries-backup.jsonl, soft-deleted through the sync API, and replaced
with the new list; then notify and read back. Other lists are not touched.
Refuses while the local SQLite has pending grocery rows. Dry run by default.

  python scripts/tools/build_shopping_list.py --from 2026-10-15 --to 2026-10-21 --batch "Fish Pie"
  python scripts/tools/build_shopping_list.py --from 2026-10-15 --to 2026-10-21 --batch "Fish Pie" --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from add_staples_to_groceries import (  # noqa: E402
    get_result,
    local_pending_groceries,
    post_entities,
)
from apply_weekly_favourites_to_paprika import (  # noqa: E402
    aggregate_grocery_rows,
    append_pantry_staples,
    clean_ingredient_line,
    load_pantry_staples,
    split_qty_name,
)
from cholesterol_score_lib import strip_inline_chol  # noqa: E402
from cleanup_library import scale_line  # noqa: E402
from fodmap_score_lib import (  # noqa: E402
    MAX_LINK_DEPTH,
    linked_recipe,
    linked_servings,
    parse_servings,
    recipe_resolver,
)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from paprika_grocery_aisles import aisle_for, normalize_aisle  # noqa: E402
from rescore_fodmap import tidy_amount  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
BACKUP = ROOT / "docs" / "groceries-backup.jsonl"
STAPLE_PREFIX = "Staple - "
# Recipes whose ingredients are bought as one staple, though the lines name the
# fruit itself: recipe name fragment -> the staple that covers its lines.
RECIPE_STAPLES: dict[str, str] = {}  # e.g. {"recipe name fragment": "Staple name"}
# A planned line naming one of these dishes comes from pantry stock when a
# stock item has the same word (e.g. "beef bolognese sauce" on a baked potato).
DISH_WORDS = ("bolognese", "hummus", "soup", "pie", "curry", "stew", "chilli")


def all_recipes() -> list[dict]:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select uid, name, ingredients, servings from recipes where coalesce(in_trash,0)=0")]
    con.close()
    return rows


def load_recipes(uids: set[str]) -> dict[str, dict]:
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    for uid in uids:
        r = con.execute(
            "select uid, name, ingredients, servings from recipes where uid=?", (uid,)
        ).fetchone()
        if r:
            out[uid] = dict(r)
    con.close()
    return out


def stock_names() -> set[str]:
    """Non-staple pantry items (batch-cooked meals), by name without markers."""
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        "select ingredient from pantry_items where ingredient not like ? "
        "and coalesce(in_stock,1)=1",
        (STAPLE_PREFIX + "%",),
    ).fetchall()
    con.close()
    return {strip_marker(r[0]).strip().lower() for r in rows}


def from_stock(line: str, stock: set[str]) -> bool:
    low = line.lower()
    return any(w in low and any(w in s for s in stock) for w in DISH_WORDS)


def merge_sweeteners(items: list[dict]) -> list[dict]:
    """One "Maple syrup (or sugar)" item for every maple syrup / sugar line, with
    each recipe's amount kept in the item's note."""
    sweet = [it for it in items if it.get("recipe") != "Staples"
             and re.search(r"maple syrup|\bsugar\b", it.get("name") or "", re.I)]
    if len(sweet) < 2:
        return items
    merged = dict(sweet[0], name="Maple syrup (or sugar)", ingredient="maple syrup", quantity="",
                  recipe="; ".join(dict.fromkeys(it.get("recipe") or "" for it in sweet)),
                  instruction="; ".join(f"{it.get('recipe')}: {it.get('name')}" for it in sweet),
                  recipe_uid=None)
    return [merged if it is sweet[0] else it for it in items if it is sweet[0] or it not in sweet]


def recipe_rows(r: dict, factor: float = 1.0, stock: set[str] = frozenset(),
                skipped: list[str] | None = None, resolve=None, _depth: int = 0) -> list[dict]:
    label = strip_marker(r["name"])
    rows = []
    for line in (r.get("ingredients") or "").splitlines():
        linked = linked_recipe(line, resolve) if _depth < MAX_LINK_DEPTH else None
        if linked:
            serv = parse_servings(linked.get("servings")) or 1
            rows += recipe_rows(linked, factor * linked_servings(line, linked) / serv,
                                stock, skipped, resolve, _depth + 1)
            continue
        cleaned = clean_ingredient_line(strip_inline_chol(line))
        if not cleaned:
            continue
        if from_stock(cleaned, stock):
            if skipped is not None:
                skipped.append(f"{label}: {cleaned} (from pantry stock)")
            continue
        if factor != 1.0:
            scaled = scale_line(cleaned, factor)
            cleaned = tidy_amount(scaled) if scaled else cleaned
        qty, ing = split_qty_name(cleaned)
        name = f"{qty} {ing}".strip() if qty else ing
        rows.append({
            "name": name,
            "ingredient": ing,
            "quantity": qty,
            "aisle": aisle_for(name, is_pack=False),
            "recipe": label + (f" (batch x{factor:g})" if factor != 1.0 else ""),
            "recipe_uid": r["uid"],
        })
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True, help="first meal date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", help="last meal date (default: start + 6 days)")
    ap.add_argument("--batch", action="append", default=[], help="name fragment of a batch cook")
    ap.add_argument("--portions", type=int, default=8, help="portions per batch cook")
    ap.add_argument("--extra", action="append", default=[],
                    help="name fragment of a recipe to make this week that isn't on the calendar "
                         "(e.g. a granola); added once at its own batch size")
    ap.add_argument("--meal", action="append", default=[],
                    help="name fragment of a meal to cook as a batch of --portions, "
                         "on the calendar or not (e.g. a Brick Method meal)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start + timedelta(days=6)

    user, pw = paprika_credentials()
    lim = RateLimiter(0.3)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        st, body = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
                                  data={"email": user, "password": pw})
        if st != 200:
            raise SystemExit(f"login failed {st}")
        h = {"Authorization": f"Bearer {body['result']['token']}"}

        meals = [m for m in await get_result(s, lim, h, "/v2/sync/meals/")
                 if not m.get("deleted") and m.get("recipe_uid")
                 and start.isoformat() <= (m.get("date") or "")[:10] <= end.isoformat()]
        recipes = load_recipes({m["recipe_uid"] for m in meals})
        stock = stock_names()
        library = all_recipes()
        resolve = recipe_resolver(library)

        rows: list[dict] = []
        batches_done: set[str] = set()
        skipped: list[str] = []
        for m in sorted(meals, key=lambda m: (m["date"], m.get("type") or 0)):
            r = recipes.get(m["recipe_uid"])
            if not r:
                skipped.append(f"{m['date'][:10]} {m.get('name')} (recipe not found locally)")
                continue
            label = strip_marker(r["name"])
            if any(b.lower() in r["name"].lower() for b in args.batch):
                if r["uid"] not in batches_done:
                    batches_done.add(r["uid"])
                    serv = parse_servings(r.get("servings")) or 1
                    rows += recipe_rows(r, args.portions / serv, stock, skipped, resolve)
                continue
            if label.lower() in stock:
                skipped.append(f"{m['date'][:10]} {label} (from pantry stock)")
                continue
            covering = next((st for k, st in RECIPE_STAPLES.items() if k in label.lower()), None)
            if covering:
                skipped.append(f"{m['date'][:10]} {label} (staple: {covering})")
                continue
            if label.lower().startswith("waitrose ") and "snack" not in label.lower():
                skipped.append(f"{m['date'][:10]} {label} (Waitrose ready meal staple)")
                continue
            rows += recipe_rows(r, 1.0, stock, skipped, resolve)
        missing = [b for b in args.batch
                   if not any(b.lower() in recipes[u]["name"].lower() for u in batches_done)]
        for b in missing:
            safe_print(f"WARN: batch '{b}' is not on the calendar in this range; not added")

        if args.extra:
            con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            for frag in args.extra:
                found = con.execute(
                    "select uid, name, ingredients, servings from recipes "
                    "where name like ? and coalesce(in_trash,0)=0", (f"%{frag}%",)).fetchall()
                if len(found) != 1:
                    raise SystemExit(f"--extra '{frag}' matches {len(found)} recipes; be more specific")
                rows += recipe_rows(dict(found[0]), 1.0, stock, skipped, resolve)
                safe_print(f"Extra recipe: {strip_marker(found[0]['name'])} (whole recipe)")
            con.close()

        for frag in args.meal:
            found = [r for r in library if frag.lower() in r["name"].lower()]
            if len(found) != 1:
                raise SystemExit(f"--meal '{frag}' matches {len(found)} recipes; be more specific")
            serv = parse_servings(found[0].get("servings")) or 1
            rows += recipe_rows(found[0], args.portions / serv, stock, skipped, resolve)
            safe_print(f"Meal: {strip_marker(found[0]['name'])} x{args.portions}")

        staples = load_pantry_staples()
        rows = append_pantry_staples(rows, staples)
        items = merge_sweeteners(aggregate_grocery_rows(rows))

        lists = [x for x in await get_result(s, lim, h, "/v2/sync/grocerylists/") if not x.get("deleted")]
        main_list = next((x for x in lists if x.get("is_default")), lists[0] if lists else None)
        if not main_list:
            raise SystemExit("no grocery list found")
        aisles = {a["name"]: a["uid"]
                  for a in await get_result(s, lim, h, "/v2/sync/groceryaisles/")
                  if not a.get("deleted") and a.get("name")}
        for i, it in enumerate(items):
            aisle = normalize_aisle(it.get("aisle"))
            if aisle == "Miscellaneous":
                aisle = aisle_for(it["name"])
            if aisle not in aisles:
                aisle = "Miscellaneous"
            it.update(aisle=aisle, aisle_uid=aisles.get(aisle), list_uid=main_list["uid"],
                      order_flag=i, uid=it.get("uid") or str(uuid.uuid4()).upper())
            it["quantity"] = "" if it.get("quantity") is None else str(it["quantity"])

        existing = [g for g in await get_result(s, lim, h, "/v2/sync/groceries/")
                    if not g.get("deleted") and g.get("list_uid") == main_list["uid"]]

        safe_print(f"Meals {start} to {end}: {len(meals)} calendar entries")
        for line in skipped:
            safe_print(f"  from stock: {line}")
        safe_print(f"\nNew list for '{main_list.get('name')}' ({len(items)} items; "
                   f"replaces {len(existing)} existing):")
        for aisle in sorted({it["aisle"] for it in items}):
            safe_print(f"\n  [{aisle}]")
            for it in [x for x in items if x["aisle"] == aisle]:
                tag = "STAPLE" if it.get("recipe") == "Staples" else "BUY"
                safe_print(f"    {tag:6} {it['name']}  ({it.get('recipe')})")

        if not args.apply:
            safe_print("\nDry run only. Pass --apply to replace the list.")
            return
        if local_pending_groceries():
            raise SystemExit("Local SQLite has pending grocery rows; see docs/desktop-sync-troubleshooting.md")

        with open(BACKUP, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"list": main_list["uid"], "items": existing}, ensure_ascii=False) + "\n")
        for i in range(0, len(existing), 20):
            if not await post_entities(s, lim, h, "/v2/sync/groceries/",
                                       [dict(g, deleted=True) for g in existing[i:i + 20]]):
                raise SystemExit(f"removing old items failed at {i}")
        for i in range(0, len(items), 20):
            if not await post_entities(s, lim, h, "/v2/sync/groceries/", items[i:i + 20]):
                raise SystemExit(f"adding items failed at {i}")
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as r:
            safe_print(f"notify {r.status}")
        live = {g["uid"] for g in await get_result(s, lim, h, "/v2/sync/groceries/") if not g.get("deleted")}
        safe_print(f"Removed {len(existing)}, added {len(items)}; "
                   f"read-back {sum(it['uid'] in live for it in items)}/{len(items)} present, "
                   f"{sum(g['uid'] in live for g in existing)} old still live")


if __name__ == "__main__":
    asyncio.run(main())
