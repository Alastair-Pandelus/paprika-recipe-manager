"""
Find and optionally deduplicate Paprika recipes.

Duplicate signals (strongest first):
  1. Same non-empty source_url
  2. Same normalized name + identical ingredients fingerprint
  3. Same normalized name + same website category

On --apply: keep the richest copy, merge categories onto it, trash the rest.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

APPLY = "--apply" in sys.argv
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

WEBSITE_NAMES = {
    "Curated",
    "Desiree Nielsen",
    "Feed Me Phoebe",
    "Field Doctor",
    "Fodmap Tracker App",
    "FODY Foods",
    "Fun Without FODMAPs",
    "Karlijn's Kitchen",
    "Monash",
}


def safe_print(*a, **k) -> None:
    try:
        print(*a, **k, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), **k, flush=True)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def norm_name(name: str) -> str:
    s = (name or "").lower()
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s)
    # strip common trailing fluff for matching
    s = re.sub(r"\s*\|.*$", "", s)
    s = re.sub(r"\s*\(.*?low\s*fodmap.*?\)\s*$", "", s, flags=re.I)
    s = re.sub(r"[^a-z0-9\s'&+-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_url(url: str) -> str:
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = u.rstrip("/")
    u = re.sub(r"[?#].*$", "", u)
    return u


def ing_fp(ingredients: str) -> str:
    text = (ingredients or "").lower()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 20:
        return ""
    return hashlib.md5(text.encode()).hexdigest()


def richness(rec: dict) -> tuple:
    photo = 1 if rec.get("photo") or rec.get("photo_hash") or rec.get("image_url") else 0
    ings = len(rec.get("ingredients") or "")
    dirs = len(rec.get("directions") or "")
    cats = len(rec.get("categories") or [])
    desc = len(rec.get("description") or "")
    # prefer older created? or newer — prefer more complete
    return (photo, ings + dirs + desc, cats, len(rec.get("name") or ""))


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


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            cats = [c for c in (await r.json())["result"] if not c.get("deleted")]
        cat_by_uid = {c["uid"].lower(): c for c in cats}
        website_uids = {
            c["uid"].lower()
            for c in cats
            if c.get("name") in WEBSITE_NAMES
        }

        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]

        sem = asyncio.Semaphore(6)

        async def load(uid: str) -> dict:
            async with sem:
                for attempt in range(6):
                    async with s.get(
                        f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
                    ) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        return (await resp.json(content_type=None)).get("result") or {}
            return {}

        recipes: list[dict] = []
        for start in range(0, len(index), 50):
            batch = await asyncio.gather(
                *(load(e["uid"]) for e in index[start : start + 50])
            )
            for rec in batch:
                if not rec or rec.get("in_trash"):
                    continue
                recipes.append(rec)

        safe_print(f"Active recipes: {len(recipes)}")

        by_url: dict[str, list[dict]] = defaultdict(list)
        by_name: dict[str, list[dict]] = defaultdict(list)
        by_name_ing: dict[tuple[str, str], list[dict]] = defaultdict(list)
        by_name_site: dict[tuple[str, str], list[dict]] = defaultdict(list)

        for rec in recipes:
            url = norm_url(rec.get("source_url") or rec.get("url") or "")
            if url:
                by_url[url].append(rec)
            nn = norm_name(rec.get("name") or "")
            if nn:
                by_name[nn].append(rec)
                fp = ing_fp(rec.get("ingredients") or "")
                if fp:
                    by_name_ing[(nn, fp)].append(rec)
                sites = [
                    cu
                    for cu in (str(x).lower() for x in (rec.get("categories") or []))
                    if cu in website_uids
                ]
                for site in sites or ["(none)"]:
                    by_name_site[(nn, site)].append(rec)

        url_dupes = {k: v for k, v in by_url.items() if len(v) > 1}
        name_ing_dupes = {k: v for k, v in by_name_ing.items() if len(v) > 1}
        name_site_dupes = {k: v for k, v in by_name_site.items() if len(v) > 1}
        name_only = {k: v for k, v in by_name.items() if len(v) > 1}

        safe_print(f"\nSame source URL: {len(url_dupes)} groups "
                   f"({sum(len(v)-1 for v in url_dupes.values())} extras)")
        safe_print(f"Same name + same ingredients: {len(name_ing_dupes)} groups "
                   f"({sum(len(v)-1 for v in name_ing_dupes.values())} extras)")
        safe_print(f"Same name + same website folder: {len(name_site_dupes)} groups "
                   f"({sum(len(v)-1 for v in name_site_dupes.values())} extras)")
        safe_print(f"Same normalized name (any source): {len(name_only)} groups "
                   f"({sum(len(v)-1 for v in name_only.values())} extras)")

        # Build unique trash set: auto-dedupe strong signals only
        # reason -> list of (keep, trash_list)
        actions: list[tuple[str, dict, list[dict]]] = []
        seen_trash: set[str] = set()
        seen_keep: set[str] = set()

        def queue_group(reason: str, group: list[dict]) -> None:
            group = sorted(group, key=richness, reverse=True)
            keep = group[0]
            trash = []
            for r in group[1:]:
                uid = r["uid"]
                if uid in seen_trash or uid == keep["uid"]:
                    continue
                if uid in seen_keep:
                    # already chosen as keeper elsewhere — skip trashing
                    continue
                trash.append(r)
                seen_trash.add(uid)
            if trash:
                seen_keep.add(keep["uid"])
                actions.append((reason, keep, trash))

        for url, group in sorted(url_dupes.items(), key=lambda x: -len(x[1])):
            queue_group(f"url:{url}", group)

        for (nn, fp), group in sorted(name_ing_dupes.items(), key=lambda x: -len(x[1])):
            # skip if already fully covered
            if all(r["uid"] in seen_trash or r["uid"] in seen_keep for r in group[1:]):
                continue
            queue_group(f"name+ings:{nn}", group)

        for (nn, site), group in sorted(name_site_dupes.items(), key=lambda x: -len(x[1])):
            if all(r["uid"] in seen_trash or r["uid"] in seen_keep for r in group[1:]):
                continue
            site_name = cat_by_uid.get(site, {}).get("name", site)
            queue_group(f"name+site:{nn}|{site_name}", group)

        safe_print(f"\n=== AUTO-DEDUPE CANDIDATES ({len(actions)} groups, "
                   f"{sum(len(t) for _,_,t in actions)} to trash) ===")
        for reason, keep, trash in actions[:80]:
            safe_print(f"\nKEEP: {keep.get('name')}")
            safe_print(f"  reason: {reason}")
            for t in trash:
                safe_print(f"  TRASH: {t.get('name')}  uid={t['uid'][:8]}…")
        if len(actions) > 80:
            safe_print(f"\n… and {len(actions)-80} more groups")

        # Name-only groups that are NOT auto-deduped (different sites / different ings)
        ambiguous = []
        for nn, group in sorted(name_only.items(), key=lambda x: (-len(x[1]), x[0])):
            leftover = [
                r
                for r in group
                if r["uid"] not in seen_trash
            ]
            # collapse to unique keepers still sharing the name
            if len(leftover) > 1:
                ambiguous.append((nn, leftover))

        safe_print(f"\n=== SAME NAME LEFT (not auto-trashed): {len(ambiguous)} groups ===")
        for nn, group in ambiguous[:40]:
            safe_print(f"\n[{len(group)}x] {nn}")
            for r in group:
                url = norm_url(r.get("source_url") or r.get("url") or "") or "(no url)"
                sites = [
                    cat_by_uid.get(str(x).lower(), {}).get("name", "?")
                    for x in (r.get("categories") or [])
                    if str(x).lower() in website_uids
                ]
                safe_print(
                    f"  - {r.get('name')} | sites={sites or ['—']} | {url[:70]}"
                )
        if len(ambiguous) > 40:
            safe_print(f"\n… and {len(ambiguous)-40} more name collisions")

        if not APPLY:
            safe_print("\nDry-run only. Re-run with --apply to trash extras "
                       "(merge categories onto keeper).")
            return

        ok_k = ok_t = fail = 0
        for reason, keep, trash in actions:
            # merge all categories from trash into keep
            cats_set = {str(x).lower(): x for x in (keep.get("categories") or [])}
            for t in trash:
                for c in t.get("categories") or []:
                    if str(c).lower() not in cats_set:
                        cats_set[str(c).lower()] = c
            keep["categories"] = list(cats_set.values())
            keep["hash"] = calc_hash(keep)
            if await post_recipe(s, headers, keep):
                ok_k += 1
            else:
                fail += 1
                safe_print(f"FAIL keep update: {keep.get('name')}")
                continue
            for t in trash:
                t["in_trash"] = True
                t["hash"] = calc_hash(t)
                if await post_recipe(s, headers, t):
                    ok_t += 1
                    safe_print(f"trashed: {t.get('name')} ({reason.split(':')[0]})")
                else:
                    fail += 1
                    safe_print(f"FAIL trash: {t.get('name')}")

        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

        safe_print(
            f"\nDone. keepers_updated={ok_k} trashed={ok_t} failed={fail} "
            f"ambiguous_name_groups_left={len(ambiguous)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
