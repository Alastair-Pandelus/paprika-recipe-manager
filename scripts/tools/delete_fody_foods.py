"""Trash all recipes in the FODY Foods folder, then delete the folder."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

APPLY = "--apply" in sys.argv
FOLDER_NAMES = {"fody foods", "fody food"}


def is_fody_folder(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in FOLDER_NAMES or n.startswith("fody foods")


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def save_recipe(session, limiter, headers, rec: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(rec),
        content_type="application/octet-stream",
        filename="data",
    )
    await limiter.wait_turn()
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{rec['uid']}/", headers=headers, data=form
    ) as r:
        return '"result":true' in (await r.text()).replace(" ", "")


async def main() -> None:
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.4)
    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/categories/", headers=H
        )
        cats = [c for c in body["result"] if not c.get("deleted")]
        fody_cats = [c for c in cats if is_fody_folder(c.get("name") or "")]
        safe_print("Fody folders found:")
        for c in fody_cats:
            safe_print(f"  {c.get('name')}  {c.get('uid')}  parent={c.get('parent_uid')}")

        if not fody_cats:
            safe_print("No Fody folder found — aborting.")
            return

        folder_uids = {c["uid"].lower() for c in fody_cats}

        st, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=H
        )
        index = body["result"]
        safe_print(f"Index={len(index)} MODE={'APPLY' if APPLY else 'DRY'}")

        to_trash: list[dict] = []
        for i, e in enumerate(index, 1):
            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{e['uid']}/", headers=H
            )
            rec = (body or {}).get("result") or {}
            if not rec or rec.get("in_trash"):
                continue
            cats_l = [c.lower() for c in (rec.get("categories") or [])]
            if folder_uids.intersection(cats_l):
                to_trash.append(rec)
            if i % 100 == 0:
                safe_print(f"… scanned {i}/{len(index)} matched={len(to_trash)}")

        safe_print(f"Recipes to trash: {len(to_trash)}")
        for rec in to_trash[:20]:
            safe_print(f"  • {rec.get('name')}")
        if len(to_trash) > 20:
            safe_print(f"  … and {len(to_trash) - 20} more")

        if not APPLY:
            safe_print("Dry run only. Re-run with --apply to trash + delete folder.")
            return

        trashed = failed = 0
        for rec in to_trash:
            name = rec.get("name") or rec["uid"]
            rec["in_trash"] = True
            rec["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec["hash"] = calc_hash(rec)
            if await save_recipe(s, limiter, H, rec):
                trashed += 1
                safe_print(f"TRASH [{trashed}] {name}")
            else:
                failed += 1
                safe_print(f"FAIL {name}")

        # Delete folder(s)
        deleted_folders = 0
        for cat in fody_cats:
            payload = [{**cat, "deleted": True}]
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(payload),
                content_type="application/octet-stream",
                filename="data",
            )
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/categories/", headers=H, data=form
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            safe_print(f"Delete folder {cat.get('name')}: ok={ok}")
            if ok:
                deleted_folders += 1

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            safe_print(f"notify: {r.status}")

        safe_print(
            f"Done. trashed={trashed} failed={failed} folders_deleted={deleted_folders}"
        )


if __name__ == "__main__":
    asyncio.run(main())
