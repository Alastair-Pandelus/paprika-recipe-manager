"""Fetch vegan chilli recipe for FODMAP review."""
import asyncio
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402


async def main() -> None:
    user, password = paprika_credentials()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        async with s.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            tok = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {tok}"}
        async with s.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=H) as r:
            index = (await r.json())["result"]
        for e in index:
            async with s.get(f"{PAPRIKA_API}/v2/sync/recipe/{e['uid']}/", headers=H) as r:
                rec = (await r.json()).get("result") or {}
            if rec.get("in_trash"):
                continue
            n = (rec.get("name") or "").lower()
            if "chilli" in n or "chili" in n:
                if "vegan" in n or "veggie" in n or "vegetarian" in n or "bean" in n:
                    print("NAME", rec.get("name"))
                    print("UID", rec["uid"])
                    print("SERVINGS", rec.get("servings"))
                    print("ING")
                    print(rec.get("ingredients"))
                    print("DIR")
                    print((rec.get("directions") or "")[:2000])
                    print("NOTES")
                    print(rec.get("notes"))
                    print("CATS", rec.get("categories"))
                    print("---")


if __name__ == "__main__":
    asyncio.run(main())
