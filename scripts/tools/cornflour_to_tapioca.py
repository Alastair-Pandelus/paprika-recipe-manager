"""Swap cornflour for tapioca starch (twice the amount) in the Bay's Kitchen sauces and recipes.

Tapioca starch keeps a smooth texture after freezing and reheating; cornflour can turn
spongy. Ingredient lines are doubled and renamed; method text says tapioca starch; a
"Changed:" note is added. Dry run unless --apply.

  python scripts/tools/cornflour_to_tapioca.py [--apply]
"""
from __future__ import annotations

import asyncio
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import cholesterol_score_lib as c  # noqa: E402
import fodmap_score_lib as f  # noqa: E402
from cleanup_library import LOCAL_DB, TAG_RE, calc_hash, scale_line  # noqa: E402
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json  # noqa: E402
from rescore_fodmap import tidy_amount  # noqa: E402

CORN = re.compile(r"\b(?:corn ?flour|corn ?starch)\b", re.I)
NOTE = ("Changed: tapioca starch instead of cornflour (about twice the amount), as it stays smooth after freezing and "
        "reheating. Stir it in near the end and don't boil hard afterwards, or it thins again.")


def swap_line(line: str) -> str:
    body = TAG_RE.sub("", line).rstrip()
    head = re.split(r"[,(]", body, 1)[0]
    if not CORN.search(head):  # only when the ingredient itself is cornflour
        return line
    doubled = scale_line(body, 2) if re.match(r"^\s*[\d½¼¾⅓⅔]", body) else body
    new = tidy_amount(doubled) if doubled else body
    new = re.sub(r"^\s*3 tsp\b", "1 tbsp", new)
    return CORN.sub("tapioca starch", new)


async def main() -> None:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(f"file:{LOCAL_DB.as_posix()}?mode=ro", uri=True)
    assert con.execute("select count(*) from recipes where coalesce(is_synced,1)=0").fetchone()[0] == 0
    uids_file = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--uids=")), None)
    if uids_file:  # a reviewed list (e.g. the recipes that use it to thicken)
        wanted = json.loads(Path(uids_file).read_text())
        rows = [con.execute("select uid, name from recipes where uid=?", (u,)).fetchone() for u in wanted]
        rows = [r for r in rows if r]
    else:
        rows = con.execute("select uid, name from recipes where coalesce(in_trash,0)=0 and name like '%Bay''s Kitchen%' "
                           "and (ingredients like '%cornflour%' or ingredients like '%corn flour%' or ingredients like '%cornstarch%' "
                           "or ingredients like '%corn starch%')").fetchall()
    print(len(rows), "recipes use cornflour")
    user, pw = paprika_credentials()
    lim = RateLimiter(0.4)
    done = 0
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, "POST", f"{PAPRIKA_API}/v1/account/login", data={"email": user, "password": pw})
        if "result" not in (b or {}):
            raise SystemExit(f"login refused: {b}")
        h = {"Authorization": f"Bearer {b['result']['token']}"}
        for uid, name in rows:
            url = f"{PAPRIKA_API}/v2/sync/recipe/{uid}/"
            st, b = await api_json(s, lim, "GET", url, headers=h); r = b["result"]
            old = r["ingredients"].split("\n")
            new = [swap_line(l) for l in old]
            changes = [(TAG_RE.sub("", o).strip(), TAG_RE.sub("", n).strip()) for o, n in zip(old, new) if o != n]
            if not changes:
                continue
            directions = CORN.sub("tapioca starch", r.get("directions") or "")
            notes = (r.get("notes") or "").rstrip()
            if NOTE not in notes:
                notes += "\n" + NOTE
            done += 1
            print(name[:70]); [print("    ", o[:60], "->", n[:60]) for o, n in changes]
            if not apply:
                continue
            t = f.transform_recipe(r["name"], "\n".join(new), r.get("description") or "", notes, servings=r.get("servings"))
            t2 = c.transform_recipe(t["name"], t["ingredients"], t["description"], notes, servings=r.get("servings"))
            with open(ROOT / "docs" / "audit-backup.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: r.get(k) for k in ("uid", "name", "ingredients", "directions", "notes")}, ensure_ascii=False) + "\n")
            upd = dict(r, name=t2["name"], ingredients=t2["ingredients"], description=t2["description"], directions=directions, notes=notes)
            upd["hash"] = calc_hash(upd)
            form = aiohttp.FormData(); form.add_field("data", gzip.compress(json.dumps(upd, separators=(",", ":")).encode()), content_type="application/octet-stream", filename="data")
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(" ", ""), name
            st, b = await api_json(s, lim, "GET", url, headers=h)
            assert b["result"]["ingredients"] == upd["ingredients"], name
            if t2["name"] != r["name"]:
                print("   marker", r["name"], "->", t2["name"])
        if apply and done:
            await lim.wait_turn()
            async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=h) as resp:
                print("notify", resp.status)
    print("recipes changed:", done)


if __name__ == "__main__":
    asyncio.run(main())
