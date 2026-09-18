"""Trial FODMAP emoji score for Field Doctor Beef Bolognese + Penne → Paprika notes."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

UID = "E47DB200-625A-488F-AA9B-1E4A7EBB3178"
DRYRUN = (
    Path(__file__).resolve().parent
    / ".fd_single_serve_dryrun"
    / f"{UID}.json"
)
LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
APPLY = "--apply" in sys.argv

# Monash-style green guides (g). Confirm in app.
# types: FRU excess fructose, FRT fructans, LAC lactose, GOS, SOR sorbitol, MAN mannitol
INGREDIENT_MAP = [
    # (match substrings in name lower, grams from scrape, green_g, type_code, label)
    (["penne", "pasta"], "brown_rice_pasta", 150, "FRT", "GF penne"),
    (["chopped tomato", "plum tomato"], "canned_tomato", 100, "FRU", "plum tomato"),
    (["tomato puree", "tomato paste"], "tomato_paste", 28, "FRU", "tomato puree"),
    (["sundried"], "sundried", 8, "FRU", "sundried tomato"),
    (["red pepper"], "red_pepper", 43, "FRU", "red pepper"),
    (["fennel"], "fennel", 48, "FRT", "fennel"),
    (["carrot"], "carrot", 75, None, "carrot"),  # typically none — skip if green
    (["spinach"], "spinach", 150, None, "spinach"),
    (["parmigiano", "parmesan", "italian hard cheese"], "parmesan", 40, "LAC", "Parmesan"),
    (["olive"], "olives", 30, None, "olives"),
]

EMOJI = {
    0: "🟢",  # ≤0.25
    1: "🟡",  # ≤0.50
    2: "🟠",  # ≤0.75
    3: "🔴",  # >0.75
}

BLOCK_START = "FODMAP review (1 serve)"
BLOCK_END = "— end FODMAP review —"


def emoji_for(ratio: float) -> str:
    if ratio <= 0.25:
        return EMOJI[0]
    if ratio <= 0.50:
        return EMOJI[1]
    if ratio <= 0.75:
        return EMOJI[2]
    return EMOJI[3]


def meal_emoji(ratio: float) -> str:
    # stacked load vs 1.0 green-equivalent budget for that type
    if ratio <= 0.5:
        return EMOJI[0]
    if ratio <= 1.0:
        return EMOJI[1]
    if ratio <= 1.5:
        return EMOJI[2]
    return EMOJI[3]


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def match_detail(details: list[dict]) -> list[dict]:
    rows = []
    used = set()
    for keys, _id, green, ftype, label in INGREDIENT_MAP:
        for i, it in enumerate(details):
            if i in used:
                continue
            n = (it.get("name") or "").lower()
            if any(k in n for k in keys):
                used.add(i)
                g = float(it["grams"])
                ratio = g / green if green else 0
                rows.append(
                    {
                        "label": label,
                        "display": it.get("display") or label,
                        "grams": round(g, 1),
                        "green": green,
                        "type": ftype,
                        "ratio": ratio,
                        "emoji": emoji_for(ratio) if ftype else "🟢",
                    }
                )
                break
    return rows


def build_block(rows: list[dict]) -> str:
    # Only show ingredients with a FODMAP type worth tracking (not green band)
    shown = [
        r for r in rows if r["type"] and r["ratio"] >= 0.15 and r["emoji"] != "🟢"
    ]
    stacks: dict[str, float] = {}
    for r in shown:
        stacks[r["type"]] = stacks.get(r["type"], 0) + r["ratio"]

    lines = [
        BLOCK_START,
        "Legend: 🟢 ≤¼ green  🟡 ≤½  🟠 ≤¾  🔴 >¾ (of that food's green serve)",
        "Proxy only — not Monash lab Stack Cup grams. Confirm in Monash app.",
        "",
        "Ingredients (tracked):",
    ]
    for r in sorted(shown, key=lambda x: -x["ratio"]):
        lines.append(
            f"{r['display']}  {r['type']} {r['emoji']}  "
            f"~{r['ratio']:.1f}× (~{r['grams']:g}/{r['green']} g)"
        )

    lines.append("")
    lines.append("Recipe stack (sum of × green within type):")
    type_order = ["FRU", "FRT", "LAC", "GOS", "SOR", "MAN"]
    visible = []
    for t in type_order:
        if t not in stacks:
            continue
        load = stacks[t]
        if load < 0.5:
            continue  # hide near-green types
        visible.append((t, load, meal_emoji(load)))
        lines.append(f"{t} {meal_emoji(load)}  ~{load:.1f}× stacked")

    if not visible:
        lines.append("(all types near green)")

    # Meal = worst visible type
    if visible:
        worst = max(visible, key=lambda x: x[1])
        drivers = []
        if any(t == "FRU" for t, _, _ in visible):
            drivers.append("tomato stack")
        if any(t == "FRT" for t, _, _ in visible):
            drivers.append("pasta")
        lines.append("")
        lines.append(
            f"Meal {worst[2]}  "
            + (" · ".join(drivers) if drivers else f"driven by {worst[0]}")
        )
        hidden = [t for t in type_order if t not in stacks or stacks[t] < 0.5]
        if hidden:
            lines.append("Hidden (green): " + " ".join(hidden))

    lines.append(BLOCK_END)
    return "\n".join(lines)


def upsert_notes(notes: str, block: str) -> str:
    notes = notes or ""
    # remove previous trial block
    notes = re.sub(
        rf"\n*{re.escape(BLOCK_START)}.*?{re.escape(BLOCK_END)}\n*",
        "\n",
        notes,
        flags=re.S,
    ).strip()
    if notes:
        return notes + "\n\n" + block + "\n"
    return block + "\n"


async def main() -> None:
    if not DRYRUN.is_file():
        raise SystemExit(f"Missing dry-run artifact: {DRYRUN}")
    art = json.loads(DRYRUN.read_text(encoding="utf-8"))
    rows = match_detail(art.get("ingredient_detail") or [])
    block = build_block(rows)
    safe_print(block)
    safe_print("---")

    # local notes
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    rec_local = con.execute(
        "SELECT notes FROM recipes WHERE uid=?", (UID,)
    ).fetchone()
    old_notes = (rec_local["notes"] if rec_local else "") or ""
    new_notes = upsert_notes(old_notes, block)

    if not APPLY:
        safe_print("Dry-run only (notes not written). Pass --apply to save.")
        con.close()
        return

    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
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
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=H
        )
        rec = (body or {}).get("result") or {}
        if not rec.get("uid"):
            raise SystemExit("cloud recipe missing")
        rec["notes"] = upsert_notes(rec.get("notes") or "", block)
        rec["hash"] = calc_hash(rec)
        await limiter.wait_turn()
        form = aiohttp.FormData()
        form.add_field(
            "data",
            gzip_obj(rec),
            content_type="application/octet-stream",
            filename="data",
        )
        async with s.post(
            f"{PAPRIKA_API}/v2/sync/recipe/{UID}/", headers=H, data=form
        ) as r:
            ok = '"result":true' in (await r.text()).replace(" ", "")
        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            await r.text()
    if not ok:
        raise SystemExit("save failed")
    con.execute(
        "UPDATE recipes SET notes=?, status=? WHERE uid=?",
        (new_notes, "modified", UID),
    )
    con.commit()
    con.close()
    safe_print("Applied to Paprika notes + local DB.")


if __name__ == "__main__":
    asyncio.run(main())
