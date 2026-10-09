"""Rewrite Prep bowls that only hold food for a cold-start boil into the pot.

Rule (paprika-prep-bowls skill):
  - Fry / wait for hot oil or already-hot liquid → keep bowl
  - Cold-start boil (food in pot, then water/stock) → no bowl; prep into the pot
  - Pasta/noodles/eggs into already-boiling water → keep hold (bowl/colander OK)
  - Grains rinsed then boiled from cold → sieve + pot, no storage bowl

  python scripts/tools/fix_prep_cold_start_boil_bowls.py
  python scripts/tools/fix_prep_cold_start_boil_bowls.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
OUT = Path(__file__).resolve().parent / ".prep_cold_start_fix_dryrun.json"

BOWL_BLOCK = re.compile(
    r"\*\*((?:Large|Medium|Small) bowl — ([^*]+))\*\*\n((?:- .+\n)*)",
    re.I,
)

# Bowls that wait for already-boiling water — keep.
HOLD_BOILING = re.compile(
    r"\b(pasta|noodle|spaghetti|penne|soba|vermicelli|gnocchi|spaetzle|"
    r"lasagna noodle|eggs?|soft-boil)\b",
    re.I,
)

# Never convert these labels (not cold-start pot holds).
SKIP_LABEL = re.compile(
    r"\b(pesto|sauce|hummus|queso|marinade|avocado mash|spice mix|"
    r"batter|blend|toast|brown|crisp|soften|sear|bloom|marinate|"
    r"poach in sauce|finish|seasoning|roast|mash seasoning|"
    r"simmer in sauce|simmer in curry|simmer later|add later|"
    r"finish simmer|poach|soak / blend|"
    r"fry /|fry / brown|fry / crisp|fry / soften|fry later\)|"
    r"\(fry)\b",
    re.I,
)

# Cold-start boil candidates (roots, grains — food in pot then water).
COLD_CANDIDATE = re.compile(
    r"\b(grain|grains|rice|quinoa|millet|potato|potatoes|mash|parsnip|"
    r"turnip|sweet potato|pumpkin|lentil|lentils|broccoli|"
    r"potato topping)\b",
    re.I,
)

# Require an explicit boil (or boil/mash), not bare "mash" / "simmer later".
BOILISH = re.compile(
    r"\b(boil(?:\s*/\s*(?:mash|mix|fry later))?|boil later|"
    r"boil then fry)\b",
    re.I,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def prior_text_has_hot_step(dirs: str, bowl_start: int) -> bool:
    """True if a fry/sear/roux/soften cook step appears before this bowl is used."""
    prep_end = dirs.find("**1.")
    if prep_end < 0:
        prep_end = 0
    body = dirs[prep_end:]
    hot = re.search(
        r"\*\*\d+\.\s[^*]*(?:Brown|Sear|Fry|Soften|Roux|Toast|Bloom|Sauté|Saute|Bacon|Crisp)",
        body,
        re.I,
    )
    add = re.search(
        r"\*\*\d+\.\s[^*]*(?:Cover|Place|Boil|Build pot|Simmer|Grains|Mash|Rice|Potato)",
        body,
        re.I,
    )
    if hot and add and hot.start() < add.start():
        return True
    if hot and not add:
        return True
    if re.search(
        r"(?:soften|brown|sear|fry|toast|roux|fragrant|bacon|crisp).{0,400}"
        r"(?:add the |stir in the ).{0,40}bowl",
        body,
        re.I | re.S,
    ):
        return True
    return False


def label_key(heading_inner: str) -> str:
    m = re.match(r"([^(]+)", heading_inner)
    raw = (m.group(1) if m else heading_inner).strip().lower()
    # first meaningful token
    for tok in raw.replace("&", " ").split():
        if tok not in {"the", "and", "or"}:
            return tok
    return raw.split()[0] if raw else "food"


def rewrite_dirs(dirs: str) -> tuple[str, list[str]]:
    if "Prep (bowls)" not in dirs and "Prep (bowls):" not in (dirs or ""):
        return dirs, []
    changes: list[str] = []
    new = dirs
    # Work on a copy; replace bowl blocks from end to start so offsets stay valid
    blocks = list(BOWL_BLOCK.finditer(dirs))
    for m in reversed(blocks):
        full_heading = m.group(1).strip()
        inner = m.group(2).strip()
        bullets = m.group(3)
        if HOLD_BOILING.search(inner):
            continue
        if SKIP_LABEL.search(inner):
            continue
        if not BOILISH.search(inner):
            continue
        if not COLD_CANDIDATE.search(inner):
            continue
        # toast / simmer risotto rice needs bowl for oil toast
        if re.search(r"\btoast\b", inner, re.I):
            continue
        # Rice/quinoa that simmers in a paella/risotto after frying stay in a bowl
        if re.search(r"\b(simmer)\b", inner, re.I) and not re.search(
            r"\bboil\b", inner, re.I
        ):
            if prior_text_has_hot_step(dirs, m.start()):
                continue
        # Potatoes/broccoli that join after sausage/roux/fry → keep bowl
        if re.search(r"\b(potato|potatoes|broccoli|lentil|greens)\b", inner, re.I):
            if prior_text_has_hot_step(dirs, m.start()) and not re.search(
                r"\b(boil / mash|boil later|boil)\b", inner, re.I
            ):
                continue
            # Explicit boil/mash from cold still converts even if other fry bowls exist
            if prior_text_has_hot_step(dirs, m.start()) and re.search(
                r"\bsimmer\b", inner, re.I
            ) and not re.search(r"\bboil\b", inner, re.I):
                continue

        key = label_key(inner)
        # Any rice/quinoa/grain boil → grains pot (incl. "rice & broccoli").
        is_grain = bool(
            re.search(r"\b(grain|grains|rice|quinoa|millet)\b", inner, re.I)
        )
        # Build replacement block
        if is_grain:
            # Grains: rinse in sieve → pot
            new_bullets = []
            for line in bullets.splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                text = line[1:].strip()
                text = re.sub(
                    r"(?i)^rinse (the )?",
                    "Rinse ",
                    text,
                )
                if re.search(r"(?i)rinse", text):
                    new_bullets.append(
                        f"- {text.rstrip('.')} in a sieve; tip into the pot with salted water."
                    )
                else:
                    new_bullets.append(
                        f"- {text.rstrip('.')} — rinse in a sieve if needed; cook in the pot with salted water."
                    )
            if not new_bullets:
                new_bullets = [
                    "- Rinse the grains in a sieve; tip into the pot with salted water."
                ]
            replacement = (
                "**Prep (into the grains pot):**\n" + "\n".join(new_bullets) + "\n"
            )
            changes.append(f"{full_heading} → into the grains pot")
        else:
            new_bullets = []
            for line in bullets.splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                text = line[1:].strip()
                text = re.sub(r"(?i)\bbeside this bowl\b", "by the hob", text)
                text = re.sub(r"(?i)\bthis bowl\b", "the pot", text)
                if not re.search(r"(?i)straight into the pot|into the pot", text):
                    if re.search(
                        r"(?i)^(peel|cut|dice|cube|slice|have|chop|drain|rinse|pat)",
                        text,
                    ):
                        text = text.rstrip(".") + " (go straight into the pot)."
                new_bullets.append(f"- {text}")
            if not new_bullets:
                continue
            pot_title = (
                "**Into the soup pot:**\n"
                if re.search(r"(?i)soup|simmer|chicken|lentil|broth", inner)
                else "**Into the boiling pot:**\n"
            )
            replacement = pot_title + "\n".join(new_bullets) + "\n"
            changes.append(f"{full_heading} → {pot_title.strip()}")

        new = new[: m.start()] + replacement + new[m.end() :]

        # Fix cook-step references for this label
        # grains bowl / rice bowl / quinoa bowl / mash bowl / potatoes bowl etc.
        patterns = [
            (
                rf"\bthe {re.escape(key)} bowl\b",
                f"the {key} in the pot"
                if not is_grain
                else "the grains in the pot",
            ),
            (
                rf"\b{re.escape(key)} bowl\b",
                f"the {key} in the pot"
                if not is_grain
                else "the grains in the pot",
            ),
        ]
        if is_grain:
            patterns.extend(
                [
                    (r"\bthe grains bowl\b", "the grains in the pot"),
                    (r"\bgrains bowl\b", "the grains in the pot"),
                    (r"\bthe rice bowl\b", "the rice in the pot"),
                    (r"\brice bowl\b", "the rice in the pot"),
                    (r"\bthe quinoa bowl\b", "the quinoa in the pot"),
                    (r"\bCook the grains bowl\b", "Cook the grains in the pot"),
                    (r"\bCook the rice bowl\b", "Cook the rice in the pot"),
                ]
            )
        else:
            patterns.extend(
                [
                    (
                        rf"Cover the {re.escape(key)} bowl with",
                        f"Cover the {key} in the pot with",
                    ),
                    (
                        rf"Boil the {re.escape(key)} bowl",
                        f"Boil the {key} in the pot",
                    ),
                    (
                        rf"Place the {re.escape(key)} bowl in",
                        f"Place the {key} in",
                    ),
                    (
                        rf"Add the {re.escape(key)} bowl",
                        f"Add the {key} already in the pot"
                        if False
                        else f"Use the {key} in the pot",
                    ),
                ]
            )
        for pat, repl in patterns:
            new2, nsub = re.subn(pat, repl, new, flags=re.I)
            if nsub:
                new = new2

    # Clean doubled awkward phrases
    new = re.sub(r"\bthe the\b", "the", new)
    new = re.sub(
        r"Use the (veg|potato|potatoes|chicken|root veg) in the pot\.",
        r"Continue with the \1 already in the pot.",
        new,
        flags=re.I,
    )
    return new, changes


def list_candidates() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute(
        """
        SELECT uid, name, directions, ingredients FROM recipes
        WHERE coalesce(in_trash,0)=0 AND directions LIKE '%Prep (bowls)%'
        ORDER BY name COLLATE NOCASE
        """
    ):
        new_dirs, changes = rewrite_dirs(r["directions"] or "")
        if changes and new_dirs != (r["directions"] or ""):
            out.append(
                {
                    "uid": r["uid"],
                    "name": r["name"],
                    "changes": changes,
                    "directions": new_dirs,
                    "old_directions": r["directions"],
                }
            )
    con.close()
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cands = list_candidates()
    OUT.write_text(
        json.dumps(
            [
                {"uid": c["uid"], "name": c["name"], "changes": c["changes"]}
                for c in cands
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    safe_print(f"Candidates to fix: {len(cands)} (details in {OUT.name})")
    for c in cands:
        safe_print("-", (c["name"] or "")[:60], "|", "; ".join(c["changes"])[:100])

    if not args.apply:
        safe_print("Dry-run only. Pass --apply to write.")
        return

    user, pw = paprika_credentials()
    lim = RateLimiter(0.28)
    updated = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session,
            lim,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}
        for c in cands:
            st, body = await api_json(
                session,
                lim,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{c['uid']}/",
                headers=headers,
            )
            recipe = (body or {}).get("result") or {}
            if not recipe.get("uid"):
                safe_print("FAIL load", c["name"])
                continue
            recipe["directions"] = c["directions"]
            recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe["hash"] = calc_hash(recipe)
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(recipe),
                content_type="application/octet-stream",
                filename="data",
            )
            await lim.wait_turn()
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
                headers=headers,
                data=form,
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")
            if ok:
                updated += 1
                con = sqlite3.connect(LOCAL_DB)
                con.execute(
                    "UPDATE recipes SET directions=?, status=? WHERE uid=?",
                    (c["directions"], "modified", c["uid"]),
                )
                con.commit()
                con.close()
                safe_print("OK", (c["name"] or "")[:65])
            else:
                safe_print("FAIL save", c["name"])
        await lim.wait_turn()
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
        ) as r:
            safe_print("notify", r.status)
    safe_print(f"Updated {updated}/{len(cands)}")


if __name__ == "__main__":
    asyncio.run(main())
