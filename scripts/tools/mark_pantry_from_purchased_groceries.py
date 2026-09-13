"""Mark pantry items in stock when matching groceries are purchased."""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402


def gzip_json(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def base_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s*\(Waitrose\)\s*", "", s, flags=re.I).strip()
    if s.endswith("(common)"):
        return s[: -len("(common)")].rstrip()
    if s.endswith(")") and " (" in s:
        head, _, tail = s.rpartition(" (")
        inner = tail[:-1]
        if "; " in inner or any(
            x in inner
            for x in (
                "Korma",
                "Meatballs",
                "Pie",
                "Curry",
                "Chilli",
                "Penne",
                "Risotto",
                "Pad Thai",
                "Tagine",
                "Mac + Cheese",
                "Bolognese",
                "Chicken",
                "Fish Pie",
                "Field Green",
            )
        ):
            return head.strip()
    return s


def token_key(s: str) -> str:
    s = base_name(s).lower()
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"[^a-z0-9\s+]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def matches(grocery_key: str, pantry_key: str) -> bool:
    """Match grocery name to pantry staple — prefer exact / whole-token equality."""
    if not grocery_key or not pantry_key:
        return False
    if grocery_key == pantry_key:
        return True
    # Allow grocery to be pantry name plus extra words only if pantry is a full token
    # phrase match (e.g. "ground cumin" == "ground cumin"), not substring of a longer
    # different ingredient ("tamari" must not match "tamarind extract").
    g_tokens = grocery_key.split()
    p_tokens = pantry_key.split()
    if g_tokens == p_tokens:
        return True
    # Reject if one is a strict prefix substring of a different word (tamari/tamarind)
    if grocery_key != pantry_key:
        if grocery_key in pantry_key.split() or pantry_key in grocery_key.split():
            return grocery_key == pantry_key
        # same multi-word core: "sea salt" should not match "smoked sea salt"
        if set(g_tokens).issubset(set(p_tokens)) or set(p_tokens).issubset(set(g_tokens)):
            # only if equal length token sets after ignoring waitrose-like noise — require equal
            return False
    return False


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/groceries/", headers=headers) as r:
            groceries = [g for g in (await r.json())["result"] if not g.get("deleted")]

        purchased = [g for g in groceries if g.get("purchased")]
        print(f"Groceries active={len(groceries)}, purchased={len(purchased)}")
        if not purchased:
            # also show sample of unpurchased names to help debug
            print("No purchased grocery items found. Sample grocery purchased flags:")
            for g in groceries[:15]:
                print(
                    f"  purchased={g.get('purchased')!r}  {g.get('name') or g.get('ingredient')}"
                )
            return

        for g in purchased:
            print(f"  bought: {g.get('name') or g.get('ingredient')}")

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            pantry = [p for p in (await r.json())["result"] if not p.get("deleted")]

        bought_keys = [
            token_key(g.get("name") or g.get("ingredient") or "") for g in purchased
        ]
        bought_keys = [k for k in bought_keys if k]

        updates = []
        already = []
        for item in pantry:
            raw = item.get("ingredient") or ""
            pk = token_key(raw)
            if any(matches(bk, pk) for bk in bought_keys):
                if item.get("in_stock"):
                    already.append(raw)
                    continue
                updates.append({**item, "in_stock": True})
                print(f"  pantry -> in stock: {raw}")

        print(f"\nMarking {len(updates)} pantry items in stock "
              f"({len(already)} already in stock)")

        if updates:
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_json(updates),
                content_type="application/octet-stream",
                filename="data",
            )
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers, data=form
            ) as r:
                body = await r.text()
                if '"result":true' not in body.replace(" ", ""):
                    raise SystemExit(f"pantry update failed: {body[:300]}")
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

        async with session.get(f"{PAPRIKA_API}/v2/sync/pantry/", headers=headers) as r:
            final = [p for p in (await r.json())["result"] if not p.get("deleted")]
        in_stock = sum(1 for p in final if p.get("in_stock"))
        print(f"Done. Pantry in_stock={in_stock}/{len(final)}")


if __name__ == "__main__":
    asyncio.run(main())
