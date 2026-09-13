"""
Needs LF review cleanup:
- Keep recipes whose title contains 'Low Fodmap' (any casing) and restore
  them to Desiree Nielsen or Feed Me Phoebe based on source_url.
- Trash everything else in Needs LF review.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"
HOLD_NAME = "Needs LF review"
DESIREE = "Desiree Nielsen"
PHOEBE = "Feed Me Phoebe"
TITLE_RE = re.compile(r"low[\s\-]?fodmap", re.I)


def safe_print(*args, **kwargs) -> None:
    try:
        print(*args, **kwargs, flush=True)
    except UnicodeEncodeError:
        print(
            *(str(a).encode("ascii", "replace").decode("ascii") for a in args),
            **kwargs,
            flush=True,
        )


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_recipe(session, headers, recipe: dict) -> bool:
    form = aiohttp.FormData()
    form.add_field(
        "data", gzip_obj(recipe), content_type="application/octet-stream", filename="data"
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/", headers=headers, data=form
    ) as r:
        body = await r.text()
        return '"result":true' in body.replace(" ", "")


def target_folder(rec: dict, desiree_uid: str, phoebe_uid: str) -> str | None:
    src = (rec.get("source_url") or "").lower()
    source = (rec.get("source") or "").lower()
    notes = (rec.get("notes") or "").lower()
    if "desireerd.com" in src or "desiree" in source or "desireerd.com" in notes:
        return desiree_uid
    if "feedmephoebe.com" in src or "phoebe" in source or "feedmephoebe.com" in notes:
        return phoebe_uid
    # fallback by prior note stamp / name heuristics
    if "desiree" in notes:
        return desiree_uid
    if "phoebe" in notes:
        return phoebe_uid
    return None


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as paprika:
        async with paprika.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with paprika.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]

        def find_folder(name: str) -> str:
            hit = next(
                (
                    c
                    for c in cats
                    if (c.get("name") or "") == name
                    and (c.get("parent_uid") or "").lower() == LF.lower()
                ),
                None,
            )
            if not hit:
                raise SystemExit(f"missing folder: {name}")
            return hit["uid"]

        hold_uid = find_folder(HOLD_NAME)
        desiree_uid = find_folder(DESIREE)
        phoebe_uid = find_folder(PHOEBE)
        safe_print("hold", hold_uid)
        safe_print("desiree", desiree_uid)
        safe_print("phoebe", phoebe_uid)

        async with paprika.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        hold_l = hold_uid.lower()
        recipes: list[dict] = []
        sem = asyncio.Semaphore(4)

        async def load_one(uid: str) -> None:
            async with sem:
                for attempt in range(6):
                    async with paprika.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as r:
                        if r.status == 429:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        rec = (await r.json(content_type=None)).get("result") or {}
                        break
                else:
                    return
            if rec.get("in_trash"):
                return
            cats_l = {str(c).lower() for c in (rec.get("categories") or [])}
            if hold_l in cats_l:
                recipes.append(rec)

        for start in range(0, len(index), 40):
            await asyncio.gather(*(load_one(e["uid"]) for e in index[start : start + 40]))
            await asyncio.sleep(0.4)
        safe_print(f"Needs LF review recipes: {len(recipes)}")

        restored = trashed = failed = ambiguous = 0
        for i, rec in enumerate(recipes, 1):
            name = rec.get("name") or ""
            keep = bool(TITLE_RE.search(name))
            try:
                if keep:
                    dest = target_folder(rec, desiree_uid, phoebe_uid)
                    if not dest:
                        ambiguous += 1
                        safe_print(f"[{i}/{len(recipes)}] AMBIGUOUS keep-title {name}")
                        # still remove from hold? user said restore to source —
                        # if unknown, leave in hold
                        continue
                    rec["categories"] = [dest]
                    # strip review stamp lightly
                    notes = rec.get("notes") or ""
                    notes = re.sub(
                        r"\n*\[LF review\][^\n]*(?:\nSource swap notes:[^\n]*)?",
                        "",
                        notes,
                    ).strip()
                    rec["notes"] = notes + "\n" if notes else ""
                    rec["hash"] = calc_hash(rec)
                    ok = await post_recipe(paprika, headers, rec)
                    if ok:
                        restored += 1
                        folder = DESIREE if dest == desiree_uid else PHOEBE
                        safe_print(f"[{i}/{len(recipes)}] RESTORE -> {folder}: {name}")
                    else:
                        failed += 1
                        safe_print(f"[{i}/{len(recipes)}] FAIL restore {name}")
                else:
                    rec["in_trash"] = True
                    rec["hash"] = calc_hash(rec)
                    ok = await post_recipe(paprika, headers, rec)
                    if ok:
                        trashed += 1
                        safe_print(f"[{i}/{len(recipes)}] TRASH {name}")
                    else:
                        failed += 1
                        safe_print(f"[{i}/{len(recipes)}] FAIL trash {name}")
            except Exception as ex:
                failed += 1
                safe_print(f"[{i}/{len(recipes)}] ERR {name}: {ex}")
            await asyncio.sleep(0.2)

        async with paprika.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        safe_print(
            f"\nDone. restored={restored} trashed={trashed} "
            f"ambiguous_left_in_hold={ambiguous} failed={failed} total={len(recipes)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
