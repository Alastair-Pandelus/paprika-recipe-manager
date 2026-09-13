"""Check Field Doctor / Low FODMAP category integrity and recipe category links."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp

ENV = Path(r"C:\Users\Pandelus\.paprika-mcp.env")
BASE = "https://paprikaapp.com/api"
FD = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LF = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

for line in ENV.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(
            f"{BASE}/v1/account/login",
            data={
                "email": os.environ["PAPRIKA_USERNAME"],
                "password": os.environ["PAPRIKA_PASSWORD"],
            },
        ) as r:
            tok = (await r.json())["result"]["token"]
        H = {"Authorization": f"Bearer {tok}"}

        async with session.get(f"{BASE}/v2/sync/categories/", headers=H) as r:
            cats = (await r.json())["result"]
        print("categories:")
        for c in cats:
            name = c.get("name")
            uid = c.get("uid")
            mark = ""
            if uid == FD:
                mark = " <-- FD"
            if uid == LF:
                mark = " <-- LF"
            if name and ("field" in name.lower() or "fodmap" in name.lower()):
                print(f"  {name} | {uid}{mark} | trash={c.get('in_trash')}")
        fd_cat = next((c for c in cats if c.get("uid") == FD), None)
        lf_cat = next((c for c in cats if c.get("uid") == LF), None)
        print("\nFD cat present:", bool(fd_cat), fd_cat)
        print("LF cat present:", bool(lf_cat), {k: lf_cat.get(k) for k in ("uid", "name", "in_trash")} if lf_cat else None)

        async with session.get(f"{BASE}/v2/sync/recipes/", headers=H) as r:
            index = (await r.json())["result"]

        missing_fd = []
        missing_lf = []
        trashed = []
        ok = 0
        for entry in index:
            async with session.get(
                f"{BASE}/v2/sync/recipe/{entry['uid']}/", headers=H
            ) as r:
                recipe = (await r.json()).get("result") or {}
            name = recipe.get("name") or ""
            if "Field Doctor-Style" not in name:
                continue
            if recipe.get("in_trash"):
                trashed.append(name)
                continue
            rcats = recipe.get("categories") or []
            if FD not in rcats:
                missing_fd.append(name)
            if LF not in rcats:
                missing_lf.append(name)
            if FD in rcats and LF in rcats:
                ok += 1

        print(f"\nStyle recipes with both cats: {ok}")
        print(f"Style missing FD cat: {len(missing_fd)}")
        for n in missing_fd[:20]:
            print(" ", n)
        print(f"Style missing LF cat: {len(missing_lf)}")
        for n in missing_lf[:20]:
            print(" ", n)
        print(f"Style trashed: {len(trashed)}")
        for n in trashed:
            print(" ", n)


asyncio.run(main())
