"""
One-way sync: Paprika cloud (master) → local Postgres mirror.

Paprika does NOT expose a last-touched / updated_at timestamp on recipes.
Optimisation is hash-based:

  1. GET /v2/sync/status/     — skip if recipes+categories counters unchanged
  2. GET /v2/sync/recipes/    — [{uid, hash}, ...] compare to local recipe_index
  3. GET /v2/sync/recipe/{id} — only for new / changed hashes
  4. GET /v2/sync/categories/ — upsert all (small payload)

Usage:
  python scripts/db/sync_from_paprika.py
  python scripts/db/sync_from_paprika.py --force   # ignore status counters
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from db.connection import connect, database_url  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

FORCE = "--force" in sys.argv


def _uid(value: Any) -> str | None:
    """Paprika UIDs are usually UUIDs but not always RFC-compliant."""
    if value is None or value == "":
        return None
    return str(value).strip().upper()


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    return bool(value)


async def paprika_login(session, limiter) -> dict[str, str]:
    user, password = paprika_credentials()
    st, body = await api_json(
        session,
        limiter,
        "POST",
        f"{PAPRIKA_API}/v1/account/login",
        data={"email": user, "password": password},
    )
    if st != 200 or not isinstance(body, dict):
        raise SystemExit(f"Paprika login failed: {st} {body}")
    token = body["result"]["token"]
    return {"Authorization": f"Bearer {token}"}


def load_local_hashes(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT uid, hash FROM recipe_index")
        return {str(uid).upper(): h for uid, h in cur.fetchall()}


def load_sync_counters(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT entity, remote_counter FROM sync_state")
        return {entity: int(counter) for entity, counter in cur.fetchall()}


def upsert_sync_counter(conn, entity: str, counter: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (entity, remote_counter, last_sync_at)
            VALUES (%s, %s, now())
            ON CONFLICT (entity) DO UPDATE SET
                remote_counter = EXCLUDED.remote_counter,
                last_sync_at = EXCLUDED.last_sync_at
            """,
            (entity, counter),
        )


def upsert_category(conn, cat: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO categories (
                uid, name, parent_uid, order_flag, deleted, raw_json, synced_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, now()
            )
            ON CONFLICT (uid) DO UPDATE SET
                name = EXCLUDED.name,
                parent_uid = EXCLUDED.parent_uid,
                order_flag = EXCLUDED.order_flag,
                deleted = EXCLUDED.deleted,
                raw_json = EXCLUDED.raw_json,
                synced_at = EXCLUDED.synced_at
            """,
            (
                _uid(cat["uid"]),
                _str(cat.get("name")),
                _uid(cat.get("parent_uid")),
                int(cat.get("order_flag") or 0),
                _bool(cat.get("deleted")),
                json.dumps(cat),
            ),
        )


def ensure_category_stub(conn, cat_uid: str) -> None:
    """Recipes can still reference deleted folders no longer returned by /categories/."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO categories (uid, name, deleted, raw_json, synced_at)
            VALUES (%s, %s, TRUE, '{}'::jsonb, now())
            ON CONFLICT (uid) DO NOTHING
            """,
            (cat_uid, f"(missing category {cat_uid})"),
        )


def upsert_recipe(conn, rec: dict) -> None:
    uid = _uid(rec.get("uid"))
    if not uid:
        raise ValueError("recipe missing uid")
    cat_uids = [c for c in (_uid(x) for x in (rec.get("categories") or [])) if c]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recipes (
                uid, hash, name, ingredients, directions, description, notes,
                nutritional_info, servings, servings_min, servings_max, difficulty,
                prep_time, prep_minutes, cook_time, cook_minutes, total_time,
                total_minutes, source, source_url, image_url, photo, photo_hash,
                photo_large, photo_url, scale, rating, in_trash, is_pinned,
                on_favorites, on_grocery_list, cookbook_uid, paprika_created,
                present_in_remote, raw_json, synced_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                TRUE, %s::jsonb, now()
            )
            ON CONFLICT (uid) DO UPDATE SET
                hash = EXCLUDED.hash,
                name = EXCLUDED.name,
                ingredients = EXCLUDED.ingredients,
                directions = EXCLUDED.directions,
                description = EXCLUDED.description,
                notes = EXCLUDED.notes,
                nutritional_info = EXCLUDED.nutritional_info,
                servings = EXCLUDED.servings,
                servings_min = EXCLUDED.servings_min,
                servings_max = EXCLUDED.servings_max,
                difficulty = EXCLUDED.difficulty,
                prep_time = EXCLUDED.prep_time,
                prep_minutes = EXCLUDED.prep_minutes,
                cook_time = EXCLUDED.cook_time,
                cook_minutes = EXCLUDED.cook_minutes,
                total_time = EXCLUDED.total_time,
                total_minutes = EXCLUDED.total_minutes,
                source = EXCLUDED.source,
                source_url = EXCLUDED.source_url,
                image_url = EXCLUDED.image_url,
                photo = EXCLUDED.photo,
                photo_hash = EXCLUDED.photo_hash,
                photo_large = EXCLUDED.photo_large,
                photo_url = EXCLUDED.photo_url,
                scale = EXCLUDED.scale,
                rating = EXCLUDED.rating,
                in_trash = EXCLUDED.in_trash,
                is_pinned = EXCLUDED.is_pinned,
                on_favorites = EXCLUDED.on_favorites,
                on_grocery_list = EXCLUDED.on_grocery_list,
                cookbook_uid = EXCLUDED.cookbook_uid,
                paprika_created = EXCLUDED.paprika_created,
                present_in_remote = TRUE,
                raw_json = EXCLUDED.raw_json,
                synced_at = EXCLUDED.synced_at
            """,
            (
                uid,
                _str(rec.get("hash")),
                _str(rec.get("name")),
                _str(rec.get("ingredients")),
                _str(rec.get("directions")),
                _str(rec.get("description")),
                _str(rec.get("notes")),
                _str(rec.get("nutritional_info")),
                _str(rec.get("servings")),
                _int(rec.get("servings_min")),
                _int(rec.get("servings_max")),
                _str(rec.get("difficulty")),
                _str(rec.get("prep_time")),
                _int(rec.get("prep_minutes")),
                _str(rec.get("cook_time")),
                _int(rec.get("cook_minutes")),
                _str(rec.get("total_time")),
                _int(rec.get("total_minutes")),
                _str(rec.get("source")),
                _str(rec.get("source_url")),
                rec.get("image_url"),
                rec.get("photo"),
                rec.get("photo_hash"),
                rec.get("photo_large"),
                rec.get("photo_url"),
                rec.get("scale"),
                int(rec.get("rating") or 0),
                _bool(rec.get("in_trash")),
                _bool(rec.get("is_pinned")),
                _bool(rec.get("on_favorites")),
                _bool(rec.get("on_grocery_list")),
                _uid(rec.get("cookbook_uid")),
                rec.get("created"),
                json.dumps(rec),
            ),
        )
        cur.execute("DELETE FROM recipe_categories WHERE recipe_uid = %s", (uid,))
        for cat_uid in cat_uids:
            ensure_category_stub(conn, cat_uid)
            cur.execute(
                """
                INSERT INTO recipe_categories (recipe_uid, category_uid)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (uid, cat_uid),
            )
        cur.execute(
            """
            INSERT INTO recipe_index (uid, hash, synced_at)
            VALUES (%s, %s, now())
            ON CONFLICT (uid) DO UPDATE SET
                hash = EXCLUDED.hash,
                synced_at = EXCLUDED.synced_at
            """,
            (uid, _str(rec.get("hash"))),
        )


def mark_missing_recipes(conn, remote_uids: set[str]) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT uid FROM recipes WHERE present_in_remote = TRUE")
        local = {str(u).upper() for (u,) in cur.fetchall()}
        missing = local - {u.upper() for u in remote_uids}
        if not missing:
            return 0
        cur.execute(
            """
            UPDATE recipes
            SET present_in_remote = FALSE, synced_at = now()
            WHERE uid = ANY(%s)
            """,
            (list(missing),),
        )
        cur.execute(
            "DELETE FROM recipe_index WHERE uid = ANY(%s)",
            (list(missing),),
        )
        return len(missing)


async def main() -> None:
    safe_print(f"DB: {database_url(redacted=True)}")
    safe_print(f"Started {datetime.now(timezone.utc).isoformat()}")
    limiter = RateLimiter(0.4)

    async with aiohttp.ClientSession() as session:
        headers = await paprika_login(session, limiter)

        st, body = await api_json(
            session, limiter, "GET", f"{PAPRIKA_API}/v2/sync/status/", headers=headers
        )
        if st != 200:
            raise SystemExit(f"status failed: {st} {body}")
        status = body["result"]
        remote_recipes = int(status.get("recipes") or 0)
        remote_categories = int(status.get("categories") or 0)
        safe_print(
            f"Remote status counters: recipes={remote_recipes} categories={remote_categories}"
        )

        with connect() as conn:
            local_counters = load_sync_counters(conn)
            if (
                not FORCE
                and local_counters.get("recipes") == remote_recipes
                and local_counters.get("categories") == remote_categories
            ):
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM recipes")
                    n = cur.fetchone()[0]
                if n > 0:
                    safe_print(
                        "Status counters unchanged and DB has data — nothing to do. "
                        "Pass --force to re-diff hashes anyway."
                    )
                    return

            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/categories/",
                headers=headers,
            )
            cats = body["result"]
            safe_print(f"Categories remote={len(cats)}")
            for cat in cats:
                upsert_category(conn, cat)
            conn.commit()

            st, body = await api_json(
                session, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers
            )
            index = body["result"]
            remote_map = {}
            remote_uid_original = {}
            for e in index:
                if not e.get("uid"):
                    continue
                key = str(e["uid"]).upper()
                remote_map[key] = e["hash"]
                remote_uid_original[key] = str(e["uid"])
            local_map = load_local_hashes(conn)

            to_fetch = [uid for uid, h in remote_map.items() if local_map.get(uid) != h]
            safe_print(
                f"Recipe index={len(remote_map)} local={len(local_map)} "
                f"to_fetch={len(to_fetch)}"
            )

            fetched = failed = 0
            for i, uid_key in enumerate(to_fetch, 1):
                uid = remote_uid_original[uid_key]
                st, body = await api_json(
                    session,
                    limiter,
                    "GET",
                    f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                    headers=headers,
                )
                rec = (body or {}).get("result") if isinstance(body, dict) else None
                if not rec:
                    failed += 1
                    safe_print(f"FAIL fetch {uid}")
                    continue
                try:
                    upsert_recipe(conn, rec)
                    conn.commit()
                    fetched += 1
                except Exception as ex:
                    conn.rollback()
                    failed += 1
                    safe_print(f"FAIL upsert {uid}: {ex}")
                    continue
                if i % 25 == 0:
                    safe_print(
                        f"… {i}/{len(to_fetch)} fetched={fetched} failed={failed} "
                        f"429s={limiter.hits_429}"
                    )
            conn.commit()

            removed = mark_missing_recipes(conn, set(remote_map.keys()))
            upsert_sync_counter(conn, "recipes", remote_recipes)
            upsert_sync_counter(conn, "categories", remote_categories)
            conn.commit()

        safe_print(
            f"Done. fetched={fetched} failed={failed} marked_absent={removed} "
            f"429s={limiter.hits_429}"
        )


if __name__ == "__main__":
    asyncio.run(main())
