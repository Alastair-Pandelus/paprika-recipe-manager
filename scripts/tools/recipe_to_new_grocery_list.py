"""Put one recipe, scaled to a number of servings, on a new Paprika grocery list.

Creates the list (if no list of that name exists), then adds the recipe's ingredient lines
scaled to the servings, with their aisles. Section headers ("Shared (all the sauces):")
are left out; linked recipes are expanded as in build_shopping_list.py. Other lists are
not touched. Dry run unless --apply.

  python scripts/tools/recipe_to_new_grocery_list.py --recipe "Tomato Pair" --servings 20 --list "Tomato Pair x20" [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from add_staples_to_groceries import get_result, local_pending_groceries, post_entities  # noqa: E402
from apply_weekly_favourites_to_paprika import aggregate_grocery_rows  # noqa: E402
from build_shopping_list import all_recipes, recipe_rows  # noqa: E402
from fodmap_score_lib import parse_servings  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from paprika_grocery_aisles import aisle_for, normalize_aisle  # noqa: E402
from weekly_favourites_meal_plan import strip_marker  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipe", required=True, help="unique part of the recipe name")
    ap.add_argument("--servings", type=float, required=True)
    ap.add_argument("--list", required=True, help="name of the new grocery list")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    recipes = all_recipes()
    found = [r for r in recipes if a.recipe.lower() in strip_marker(r["name"]).lower()]
    if len(found) != 1:
        raise SystemExit(f"{len(found)} recipes match {a.recipe!r}: {[r['name'] for r in found][:5]}")
    r = found[0]
    by_name = {strip_marker(x["name"]).lower(): x for x in recipes}

    def resolve(name):
        return by_name.get(strip_marker(name).lower())
    factor = a.servings / (parse_servings(r.get("servings")) or 1)
    rows = [x for x in recipe_rows(r, factor, frozenset(), [], resolve) if not x["name"].rstrip().endswith(":")]
    items = aggregate_grocery_rows(rows)
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        lists = [x for x in await get_result(s, lim, h, "/v2/sync/grocerylists/") if not x.get("deleted")]
        target = next((x for x in lists if x.get("name") == a.list), None)
        aisles = {x["name"]: x["uid"] for x in await get_result(s, lim, h, "/v2/sync/groceryaisles/")
                  if not x.get("deleted") and x.get("name")}
        new_list = target or {"uid": str(uuid.uuid4()).upper(), "name": a.list,
                              "order_flag": max((x.get("order_flag") or 0) for x in lists) + 1 if lists else 0,
                              "is_default": False, "reminders_list": "Paprika"}
        for i, it in enumerate(items):
            aisle = normalize_aisle(it.get("aisle"))
            if aisle == "Miscellaneous":
                aisle = aisle_for(it["name"])
            if aisle not in aisles:
                aisle = "Miscellaneous"
            it.update(uid=str(uuid.uuid4()).upper(), aisle=aisle, aisle_uid=aisles.get(aisle), list_uid=new_list["uid"],
                      order_flag=i, purchased=False, instruction="", separate=False,
                      quantity="" if it.get("quantity") is None else str(it["quantity"]))
        safe_print(f"{strip_marker(r['name'])}: serves {r.get('servings')} -> {a.servings:g} (x{factor:g})")
        safe_print(f"List: {a.list} ({'exists' if target else 'new'}), {len(items)} items")
        for aisle in sorted({it["aisle"] for it in items}):
            safe_print(f"  [{aisle}]")
            for it in (x for x in items if x["aisle"] == aisle):
                safe_print(f"    {it['name']}")
        if not a.apply:
            safe_print("Dry run only. Pass --apply to write.")
            return
        if local_pending_groceries():
            raise SystemExit("Local SQLite has pending grocery rows; see docs/desktop-sync-troubleshooting.md")
        if not target:
            assert await post_entities(s, lim, h, "/v2/sync/grocerylists/", [new_list]), "list create failed"
        for i in range(0, len(items), 20):
            assert await post_entities(s, lim, h, "/v2/sync/groceries/", items[i:i + 20]), "groceries post failed"
        live = [g for g in await get_result(s, lim, h, "/v2/sync/groceries/")
                if not g.get("deleted") and g.get("list_uid") == new_list["uid"]]
        safe_print(f"read back: {len(live)} items on {a.list}")
        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
            safe_print(f"notify {resp.status}")


if __name__ == "__main__":
    asyncio.run(main())
