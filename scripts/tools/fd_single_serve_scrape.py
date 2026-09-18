"""Fresh-scrape Field Doctor meals and rebuild at N portions (default 1).

Default is dry-run: writes JSON under scripts/tools/.fd_single_serve_dryrun/
and does not touch Paprika. Pass --apply to push ingredients/servings/notes.

Discovers FD recipes from local Paprika SQLite (fast), scrapes source_url,
then optionally syncs to Paprika cloud.

  python scripts/tools/fd_single_serve_scrape.py
  python scripts/tools/fd_single_serve_scrape.py --portions 1 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "field_doctor"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import rescale_density as fd  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

# Cloud category UIDs (Paprika API) — used only when ensuring tags on apply.
FD_CAT_CLOUD = fd.FD
LF_CAT_CLOUD = fd.LF
# Local SQLite category UID for "Field Doctor" (differs from cloud UID).
FD_CAT_LOCAL = "DFD7AB80-DB1E-4257-A6E9-7E9AA44E548C"
OUT_DIR = Path(__file__).resolve().parent / ".fd_single_serve_dryrun"
LOG_PATH = Path(__file__).resolve().parent / ".fd_single_serve_scrape.log"
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def list_fd_from_local() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT r.uid, r.name, r.ingredients, r.notes, r.directions,
               r.servings, r.source_url
        FROM recipes r
        JOIN recipes_to_categories rtc ON rtc.recipe_uid = r.uid
        WHERE rtc.category_uid = ? AND COALESCE(r.in_trash, 0) = 0
        ORDER BY r.name COLLATE NOCASE
        """,
        (FD_CAT_LOCAL,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        out.append(
            {
                "uid": r["uid"],
                "name": r["name"] or "",
                "ingredients": r["ingredients"] or "",
                "notes": r["notes"] or "",
                "directions": r["directions"] or "",
                "servings": r["servings"],
                "source_url": (r["source_url"] or "").strip(),
            }
        )
    return out


def rebuild_from_html(
    html: str, *, recipe_name: str, portions: int
) -> dict | None:
    raw, serving = fd.extract_from_html(html)
    if not raw:
        return None

    items = fd.merge_duplicate_ingredients(
        fd.allocate_percentages(fd.parse_ingredients(raw))
    )
    omitted_water = next((it for it in items if fd.is_plain_water(it.name)), None)
    items = [it for it in items if not fd.is_plain_water(it.name)]

    omitted_oil = None
    kept_oils: list = []
    rebuilt: list = []
    for it in items:
        if not fd.is_olive_oil(it.name):
            rebuilt.append(it)
            continue
        if fd.should_keep_olive_oil(
            it, serving_g=serving, portions=portions, recipe_name=recipe_name
        ):
            rebuilt.append(it)
            kept_oils.append(it)
        else:
            if omitted_oil is None or it.assigned > omitted_oil.assigned:
                omitted_oil = it
    items = rebuilt

    total_g = serving * portions
    detail: list[dict] = []
    for it in items:
        grams = total_g * it.assigned / 100.0
        amount, g_note, gpt = fd.format_amount(grams, it.name)
        detail.append(
            {
                "name": it.name,
                "display": amount,
                "grams": round(grams, 3),
                "assigned_pct": round(it.assigned, 4),
                "label_pct": it.pct,
                "g_note": g_note,
                "g_per_tsp": gpt,
            }
        )

    text = fd.build_ingredient_text(items, serving, portions)
    scaling = fd.build_scaling_notes(
        items,
        serving,
        portions,
        omitted_water=omitted_water,
        omitted_oil=omitted_oil,
    )
    oil_status = "kept" if kept_oils else ("omitted" if omitted_oil else "none")
    return {
        "source_raw": raw,
        "serving_g": serving,
        "portions": portions,
        "ingredients_text": text,
        "scaling_notes": scaling,
        "ingredient_detail": detail,
        "oil_status": oil_status,
        "omitted_oil": omitted_oil is not None,
    }


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        return await r.text()


async def post_recipe(session, headers, recipe: dict, limiter: RateLimiter) -> bool:
    await limiter.wait_turn()
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(recipe),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
        headers=headers,
        data=form,
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


def update_local_db(
    uid: str, ingredients: str, notes: str, servings: str, directions: str
) -> None:
    if not LOCAL_DB.is_file():
        return
    con = sqlite3.connect(LOCAL_DB)
    try:
        con.execute(
            """
            UPDATE recipes
            SET ingredients=?, notes=?, servings=?, directions=?, status=?
            WHERE uid=?
            """,
            (ingredients, notes, servings, directions, "modified", uid),
        )
        con.commit()
    finally:
        con.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--portions", type=int, default=1)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Push to Paprika + local SQLite (default is dry-run only)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N FD recipes (0 = all)",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    apply = bool(args.apply)
    portions = max(1, int(args.portions))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index_path = OUT_DIR / "_index.json"
    log_lines: list[str] = []

    mode = "APPLY" if apply else "DRY-RUN"
    safe_print(f"FD single-serve scrape | portions={portions} | {mode}")

    fd_recipes = list_fd_from_local()
    if args.limit and args.limit > 0:
        fd_recipes = fd_recipes[: args.limit]
    safe_print(f"Field Doctor recipes (local): {len(fd_recipes)}")

    results: list[dict] = []
    skipped: list[dict] = []
    updated = 0

    headers = None
    limiter = RateLimiter(0.35)
    user = pw = None
    if apply:
        fd.load_env()
        user, pw = paprika_credentials()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        if apply:
            st, body = await api_json(
                session,
                limiter,
                "POST",
                f"{PAPRIKA_API}/v1/account/login",
                data={"email": user, "password": pw},
            )
            if st != 200:
                raise SystemExit(f"login failed: {st} {body}")
            headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for local in fd_recipes:
            uid = local["uid"]
            name = local["name"]
            url = local["source_url"]
            if not url or "fielddoctor" not in url.lower():
                skipped.append({"uid": uid, "name": name, "reason": "no_fd_url"})
                safe_print(f"SKIP no url | {name}")
                continue

            try:
                html = await fetch_html(session, url)
            except Exception as e:  # noqa: BLE001
                skipped.append(
                    {"uid": uid, "name": name, "reason": f"fetch_error: {e}"}
                )
                safe_print(f"SKIP fetch | {name} | {e}")
                continue

            rebuilt = rebuild_from_html(html, recipe_name=name, portions=portions)
            if not rebuilt:
                skipped.append({"uid": uid, "name": name, "reason": "parse_empty"})
                safe_print(f"SKIP parse | {name}")
                continue

            notes = fd.merge_recipe_notes(
                local["notes"],
                rebuilt["scaling_notes"],
                rebuilt["source_raw"],
            )
            dirs = fd.clean_directions(local["directions"])
            if rebuilt["omitted_oil"]:
                dirs = fd.ensure_fry_in_evoo(dirs)

            artifact = {
                "uid": uid,
                "name": name,
                "source_url": url,
                "old_servings": local["servings"],
                "old_ingredients": local["ingredients"],
                "new_servings": str(portions),
                "serving_g": rebuilt["serving_g"],
                "portions": portions,
                "oil_status": rebuilt["oil_status"],
                "ingredients_text": rebuilt["ingredients_text"],
                "ingredient_detail": rebuilt["ingredient_detail"],
                "notes": notes,
                "directions_changed": dirs != local["directions"],
                "directions": dirs,
                "scraped_at": datetime.now().isoformat(timespec="seconds"),
            }
            out_file = OUT_DIR / f"{uid}.json"
            out_file.write_text(
                json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            results.append(
                {
                    "uid": uid,
                    "name": name,
                    "serving_g": rebuilt["serving_g"],
                    "oil_status": rebuilt["oil_status"],
                    "ingredient_count": len(rebuilt["ingredient_detail"]),
                    "file": out_file.name,
                }
            )
            safe_print(
                f"OK {name[:52]} | {rebuilt['serving_g']}g x{portions} | oil {rebuilt['oil_status']}"
            )
            log_lines.append(
                f"{uid}\t{name}\told_serv={local['servings']}\t"
                f"new_serv={portions}\tserving_g={rebuilt['serving_g']}"
            )

            if apply:
                assert headers is not None
                st, body = await api_json(
                    session,
                    limiter,
                    "GET",
                    f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                    headers=headers,
                )
                recipe = (body or {}).get("result") or {}
                if not recipe:
                    safe_print(f"FAIL missing cloud | {name}")
                    log_lines.append(f"  FAIL missing {uid}")
                    continue
                recipe["ingredients"] = rebuilt["ingredients_text"]
                recipe["servings"] = str(portions)
                recipe["notes"] = notes
                recipe["directions"] = dirs
                # Preserve existing categories; optionally ensure cloud FD/LF UIDs.
                cats = list(recipe.get("categories") or [])
                if FD_CAT_CLOUD not in cats:
                    cats.append(FD_CAT_CLOUD)
                if LF_CAT_CLOUD not in cats:
                    cats.append(LF_CAT_CLOUD)
                recipe["categories"] = cats
                recipe["hash"] = calc_hash(recipe)
                ok = await post_recipe(session, headers, recipe, limiter)
                if ok:
                    updated += 1
                    update_local_db(
                        uid,
                        rebuilt["ingredients_text"],
                        notes,
                        str(portions),
                        dirs,
                    )
                    log_lines.append(f"  APPLIED {uid}")
                else:
                    safe_print(f"FAIL save | {name}")
                    log_lines.append(f"  FAIL {uid}")

            await asyncio.sleep(0.25)

        if apply and headers is not None:
            await limiter.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

    summary = {
        "mode": mode,
        "portions": portions,
        "scraped": len(results),
        "skipped": skipped,
        "updated": updated if apply else 0,
        "recipes": results,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    index_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    LOG_PATH.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    safe_print(
        f"Done | scraped={len(results)} skipped={len(skipped)} "
        f"updated={updated if apply else 0} | {index_path}"
    )


if __name__ == "__main__":
    asyncio.run(main())
