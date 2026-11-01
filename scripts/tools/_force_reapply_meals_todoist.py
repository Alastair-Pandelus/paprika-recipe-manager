"""Force-clear Meals on date range, re-apply plan, print Todoist JSON path."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from apply_weekly_favourites_to_paprika import (  # noqa: E402
    gzip_obj,
    meal_date_key,
    meals_from_plan,
    post_entities,
)
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402
from todoist_meals_from_plan import build_todoist_tasks  # noqa: E402
from weekly_favourites_meal_plan import build_week, load_favourites  # noqa: E402

START = date.fromisoformat("2026-10-10")
OUT = Path(__file__).resolve().parent / ".todoist_meals_week.json"


async def main() -> None:
    plan_dates = {(START + timedelta(days=i)).isoformat() for i in range(7)}
    slots = load_favourites()
    plan = build_week(START, slots)

    user, pw = paprika_credentials()
    lim = RateLimiter(0.28)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        st, body = await api_json(
            s, lim, "POST", f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, lim, "GET", f"{PAPRIKA_API}/v2/sync/mealtypes/", headers=H
        )
        types_by_name = {
            t["name"]: t
            for t in (body["result"] or [])
            if not t.get("deleted") and t.get("name")
        }
        types_by_slot = {
            "breakfast": types_by_name["Breakfast"],
            "lunch": types_by_name["Lunch"],
            "dinner": types_by_name["Dinner"],
            "pudding": types_by_name["Dessert"],
        }

        st, body = await api_json(
            s, lim, "GET", f"{PAPRIKA_API}/v2/sync/meals/", headers=H
        )
        existing = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and meal_date_key(m.get("date")) in plan_dates
        ]
        safe_print(f"Force-clearing {len(existing)} meals")
        for i in range(0, len(existing), 20):
            chunk = [{**m, "deleted": True} for m in existing[i : i + 20]]
            if not await post_entities(s, lim, H, "/v2/sync/meals/", chunk):
                raise SystemExit("delete failed")

        meal_rows = meals_from_plan(plan, types_by_slot)
        safe_print(f"Posting {len(meal_rows)} meals")
        for i in range(0, len(meal_rows), 14):
            part = meal_rows[i : i + 14]
            if not await post_entities(s, lim, H, "/v2/sync/meals/", part):
                raise SystemExit("add failed")

        await lim.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print("notify", r.status)

        st, body = await api_json(
            s, lim, "GET", f"{PAPRIKA_API}/v2/sync/meals/", headers=H
        )
        live = [
            m
            for m in (body["result"] or [])
            if not m.get("deleted") and meal_date_key(m.get("date")) in plan_dates
        ]
        safe_print(f"Live meals on plan dates: {len(live)}")

    tasks = build_todoist_tasks(START)
    OUT.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print(f"Wrote {len(tasks)} Todoist payloads → {OUT.name}")
    for t in tasks:
        safe_print(f"  {t['dueString']}: {t['content'][:80]}")


if __name__ == "__main__":
    asyncio.run(main())
