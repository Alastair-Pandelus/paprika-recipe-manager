"""Clear local Paprika pending rows that already exist on the cloud.

Fixes stuck desktop sync when meals/recipes were mirrored as status=new /
is_synced=0 even though the cloud already has the same UIDs.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OATCAKES = "9E881F68-B431-493B-A156-7D75D7390A82"


async def main() -> None:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    pending_meals = con.execute(
        """
        select uid, name, date, status, is_synced, sync_hash from meals
        where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0
        """
    ).fetchall()
    safe_print(f"Pending meals: {len(pending_meals)}")

    user, pw = paprika_credentials()
    lim = RateLimiter(0.25)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            lim,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        st, body = await api_json(
            s, lim, "GET", f"{PAPRIKA_API}/v2/sync/meals/", headers=headers
        )
        cloud_meals = {
            m["uid"]: m for m in ((body or {}).get("result") or []) if m.get("uid")
        }

        mark_meals = []
        for row in pending_meals:
            c = cloud_meals.get(row["uid"])
            if not c:
                safe_print(f"  SKIP meal not on cloud: {row['uid']} {row['name']}")
                continue
            sync_hash = c.get("hash") or row["sync_hash"]
            mark_meals.append((sync_hash, row["uid"]))
            safe_print(
                f"  MARK meal synced: {str(row['date'])[:10]} {row['name'][:50]}"
            )

        # oatcakes
        st, body = await api_json(
            s, lim, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{OATCAKES}/", headers=headers
        )
        oat = (body or {}).get("result") or {}
        oat_hash = oat.get("hash")
        safe_print(f"Oatcakes cloud hash: {oat_hash}")

    if not apply:
        safe_print("Dry-run only. Pass --apply to update local SQLite.")
        con.close()
        return

    for sync_hash, uid in mark_meals:
        con.execute(
            """
            UPDATE meals
            SET status='unmodified', is_synced=1, sync_hash=?
            WHERE uid=?
            """,
            (sync_hash, uid),
        )
    con.execute(
        """
        UPDATE recipes
        SET status='unmodified', is_synced=1, sync_hash=?
        WHERE uid=?
        """,
        (oat_hash, OATCAKES),
    )
    con.commit()

    n_meals = con.execute(
        """
        select count(*) from meals
        where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0
        """
    ).fetchone()[0]
    n_recipes = con.execute(
        """
        select count(*) from recipes
        where coalesce(status,'') != 'unmodified' or coalesce(is_synced,1)=0
        """
    ).fetchone()[0]
    con.close()
    safe_print(f"Applied. Remaining pending meals={n_meals} recipes={n_recipes}")
    safe_print("Quit Paprika fully and reopen, then sync once.")


if __name__ == "__main__":
    asyncio.run(main())
