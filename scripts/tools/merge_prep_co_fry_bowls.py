"""Merge Prep bowls that tip into the pan together in the same cook action.

Examples:
  - chicken bowl + shallot tops (fry with the chicken) → one bowl
  - cook the A bowl and B bowl until … (same tip-in)

Does NOT merge:
  - sequential adds (brown A, then add B)
  - spice mix mixed into / bloomed after
  - finish / pasta / coating tosses
  - marinade coat steps

  python scripts/tools/merge_prep_co_fry_bowls.py
  python scripts/tools/merge_prep_co_fry_bowls.py --apply
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
OUT = Path(__file__).resolve().parent / ".merge_co_fry_bowls_dryrun.json"

BOWL_BLOCK = re.compile(
    r"\*\*((Large|Medium|Small) bowl — ([^*]+?))\*\*\n((?:- .+\n)*)",
    re.I,
)

# Never merge these bowl kinds into a co-fry partner
SKIP_KIND = re.compile(
    r"\b(spice mix|marinade|coating|crust|finish|pasta|noodle|pesto|"
    r"slurry|seasoning|batter|sauce|queso|hummus)\b",
    re.I,
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def short_label(inner: str) -> str:
    """'chicken (fry / crisp):' → 'chicken'; 'shallot tops (fry with the chicken):' → 'shallot tops'."""
    core = re.sub(r"\s*\([^)]*\)\s*:?\s*$", "", inner.strip())
    core = core.strip(" :")
    return re.sub(r"\s+", " ", core).strip()


def fate_with_partner(inner: str) -> str | None:
    """If heading says 'fry with the X', return X."""
    m = re.search(
        r"\((?:fry|cook|brown|soften|crisp)[^)]*\bwith the ([^)]+)\)",
        inner,
        re.I,
    )
    if not m:
        return None
    partner = m.group(1).strip().rstrip(":")
    partner = re.sub(r"\s+", " ", partner)
    return partner.lower()


def parse_bowls(dirs: str) -> list[dict]:
    out = []
    for m in BOWL_BLOCK.finditer(dirs or ""):
        inner = m.group(3).strip()
        label = short_label(inner)
        out.append(
            {
                "full": m.group(1).strip(),
                "size": m.group(2),
                "inner": inner,
                "label": label,
                "label_l": label.lower(),
                "bullets": m.group(4),
                "start": m.start(),
                "end": m.end(),
                "with_partner": fate_with_partner(inner),
            }
        )
    return out


def find_co_fry_pairs(dirs: str, bowls: list[dict]) -> list[tuple[str, str]]:
    """Return list of (keep_label, merge_label) lowercased."""
    pairs: list[tuple[str, str]] = []
    labels = {b["label_l"]: b for b in bowls}

    # 1) Explicit 'fry with the X' headings
    for b in bowls:
        if SKIP_KIND.search(b["label_l"]) or SKIP_KIND.search(b["inner"]):
            continue
        partner = b.get("with_partner")
        if not partner:
            continue
        # partner might be 'chicken' matching bowl 'chicken'
        match = None
        for lab, other in labels.items():
            if lab == b["label_l"]:
                continue
            if partner == lab or partner.startswith(lab) or lab.startswith(partner):
                match = lab
                break
        if match:
            pairs.append((match, b["label_l"]))

    # 2) Cook line: cook/fry/brown/soften the A bowl and [the] B bowl …
    #    (same tip-in). Ignore 'mix A into B', 'toss A with B', 'coat A with B'.
    cook_body = dirs
    prep_i = dirs.find("**1.")
    if prep_i >= 0:
        cook_body = dirs[prep_i:]

    tip_pat = re.compile(
        r"(?i)(?:cook|fry|brown|soften|sear|crisp|saute|sauté|heat|stir-fry|stir fry|"
        r"add|tip(?:\s+in)?)\s+"
        r"(?:in\s+)?(?:the\s+)?([a-z0-9 &/+'-]+?)\s+bowl\s+"
        r"(?:and|with|together with)\s+(?:the\s+)?"
        r"([a-z0-9 &/+'-]+?)\s+bowl\b"
    )
    # Don't merge bowls that only meet in a marinade / coating toss
    marinade_ctx = re.compile(r"(?i)\b(marinade|marinate|coat(?:ing)?|toss)\b")
    for m in tip_pat.finditer(cook_body):
        a = re.sub(r"\s+", " ", m.group(1).strip().lower())
        b = re.sub(r"\s+", " ", m.group(2).strip().lower())
        # skip if either is spice/finish/pasta
        if SKIP_KIND.search(a) or SKIP_KIND.search(b):
            continue
        # Context around the match (line / nearby)
        span_start = max(0, m.start() - 40)
        span_end = min(len(cook_body), m.end() + 60)
        ctx = cook_body[span_start:span_end]
        if marinade_ctx.search(ctx):
            continue
        # must both be known bowls
        if a not in labels or b not in labels:
            # try prefix match
            a2 = next((lab for lab in labels if a == lab or a.startswith(lab) or lab.startswith(a)), None)
            b2 = next((lab for lab in labels if b == lab or b.startswith(lab) or lab.startswith(b)), None)
            if not a2 or not b2:
                continue
            a, b = a2, b2
        # Prefer keeping the larger / protein-named bowl
        keep, merge = choose_keep(a, b, labels)
        pairs.append((keep, merge))

    # Dedupe
    seen: set[tuple[str, str]] = set()
    uniq = []
    for keep, merge in pairs:
        if keep == merge:
            continue
        key = (keep, merge)
        if key in seen:
            continue
        # avoid cycles
        if (merge, keep) in seen:
            continue
        seen.add(key)
        uniq.append((keep, merge))
    return uniq


def choose_keep(a: str, b: str, labels: dict) -> tuple[str, str]:
    """Prefer meat/protein label as the surviving bowl name."""
    protein = re.compile(
        r"\b(chicken|beef|pork|lamb|turkey|mince|meat|tofu|fish|salmon|"
        r"prawn|shrimp|bacon|sausage)\b",
        re.I,
    )
    a_prot = bool(protein.search(a))
    b_prot = bool(protein.search(b))
    if a_prot and not b_prot:
        return a, b
    if b_prot and not a_prot:
        return b, a
    # Prefer Large over Small
    size_rank = {"Large": 0, "Medium": 1, "Small": 2}
    sa = size_rank.get(labels[a]["size"], 9)
    sb = size_rank.get(labels[b]["size"], 9)
    if sa <= sb:
        return a, b
    return b, a


def merge_dirs(dirs: str) -> tuple[str, list[str]]:
    bowls = parse_bowls(dirs)
    if len(bowls) < 2:
        return dirs, []
    pairs = find_co_fry_pairs(dirs, bowls)
    if not pairs:
        return dirs, []

    changes: list[str] = []
    new = dirs
    labels = {b["label_l"]: b for b in parse_bowls(new)}

    for keep_l, merge_l in pairs:
        labels = {b["label_l"]: b for b in parse_bowls(new)}
        if keep_l not in labels or merge_l not in labels:
            continue
        keep = labels[keep_l]
        merge = labels[merge_l]
        if SKIP_KIND.search(keep["inner"]) or SKIP_KIND.search(merge["inner"]):
            continue

        # Combined label
        keep_short = keep["label"]
        merge_short = merge["label"]
        # Avoid "chicken & chicken"
        if merge_short.lower() in keep_short.lower():
            combined_label = keep_short
        elif keep_short.lower() in merge_short.lower():
            combined_label = merge_short
        else:
            combined_label = f"{keep_short} & {merge_short}"

        # Fate from keep, drop 'with the X' from merge
        fate_m = re.search(r"\(([^)]*)\)", keep["inner"])
        fate = fate_m.group(1).strip() if fate_m else "fry"
        fate = re.sub(r"(?i)\s*with the .+$", "", fate).strip(" ;")
        if not fate:
            fate = "fry"

        size = keep["size"]
        # Prefer Large if either is Large
        if keep["size"] == "Large" or merge["size"] == "Large":
            size = "Large"
        elif keep["size"] == "Medium" or merge["size"] == "Medium":
            size = "Medium"

        # Merge bullets
        bullets = []
        for block in (keep["bullets"], merge["bullets"]):
            for line in block.splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                text = line[1:].strip()
                text = re.sub(
                    r"(?i)\badd to the .+ bowl\b",
                    f"add to the {combined_label} bowl",
                    text,
                )
                text = re.sub(
                    r"(?i)\bbeside this bowl\b",
                    f"beside the {combined_label} bowl",
                    text,
                )
                bullets.append(f"- {text}")
        # Deduplicate exact bullets
        seen_b: set[str] = set()
        uniq_b = []
        for b in bullets:
            if b in seen_b:
                continue
            seen_b.add(b)
            uniq_b.append(b)

        new_heading = f"**{size} bowl — {combined_label} ({fate}):**\n"
        replacement = new_heading + "\n".join(uniq_b) + "\n"

        # Remove merge block first (if it appears after keep, adjust)
        # Re-find current spans in `new`
        cur = parse_bowls(new)
        by = {b["label_l"]: b for b in cur}
        if keep_l not in by or merge_l not in by:
            continue
        keep = by[keep_l]
        merge = by[merge_l]

        # Delete both blocks; insert combined at keep position
        start_k, end_k = keep["start"], keep["end"]
        start_m, end_m = merge["start"], merge["end"]
        if start_m < start_k:
            # merge appears first — insert at merge, delete keep
            new = new[:start_k] + new[end_k:]
            # re-find merge after deletion if keep was before... keep was after so merge indices OK
            cur2 = parse_bowls(new)
            by2 = {b["label_l"]: b for b in cur2}
            if merge_l not in by2:
                continue
            merge = by2[merge_l]
            new = new[: merge["start"]] + replacement + new[merge["end"] :]
        else:
            new = new[:start_m] + new[end_m:]
            cur2 = parse_bowls(new)
            by2 = {b["label_l"]: b for b in cur2}
            if keep_l not in by2:
                # keep label may still be there
                # try find by old keep label
                pass
            if keep_l in by2:
                keep = by2[keep_l]
                new = new[: keep["start"]] + replacement + new[keep["end"] :]
            else:
                continue

        combined_l = combined_label.lower()
        # Rewrite cook-step refs: "the chicken bowl and shallot tops bowl" → "the chicken & shallot tops bowl"
        # Also lone "shallot tops bowl" → combined; "chicken bowl" → combined when it was the keep

        def repl_pair(text: str) -> str:
            combined_bowl = f"{combined_label} bowl"
            placeholder = "\0COMBINED_BOWL\0"
            # joint phrases first
            patterns = [
                rf"the {re.escape(keep_l)} bowl\s+(?:and|with|together with)\s+(?:the )?{re.escape(merge_l)} bowl",
                rf"the {re.escape(merge_l)} bowl\s+(?:and|with|together with)\s+(?:the )?{re.escape(keep_l)} bowl",
                rf"{re.escape(keep_l)} bowl\s+(?:and|with|together with)\s+(?:the )?{re.escape(merge_l)} bowl",
                rf"{re.escape(merge_l)} bowl\s+(?:and|with|together with)\s+(?:the )?{re.escape(keep_l)} bowl",
            ]
            for pat in patterns:
                text = re.sub(
                    pat,
                    f"the {combined_label} bowl",
                    text,
                    flags=re.I,
                )
            # Protect combined label so solo refs cannot rematch inside it
            # (e.g. "shallot tops bowl" inside "chicken & shallot tops bowl")
            text = re.sub(
                re.escape(combined_bowl),
                placeholder,
                text,
                flags=re.I,
            )
            for label in (merge_l, keep_l):
                text = re.sub(
                    rf"\bthe {re.escape(label)} bowl\b",
                    f"the {combined_label} bowl",
                    text,
                    flags=re.I,
                )
                text = re.sub(
                    rf"\b{re.escape(label)} bowl\b",
                    f"{combined_label} bowl",
                    text,
                    flags=re.I,
                )
            text = text.replace(placeholder, combined_bowl)
            return text

        new = repl_pair(new)
        # Fix "the the"
        new = re.sub(r"\bthe the\b", "the", new, flags=re.I)
        changes.append(f"{keep_short} + {merge_short} → {combined_label}")

    return new, changes


def list_candidates() -> list[dict]:
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute(
        """
        SELECT uid, name, directions FROM recipes
        WHERE coalesce(in_trash,0)=0 AND directions LIKE '%Prep (bowls)%'
        ORDER BY name COLLATE NOCASE
        """
    ):
        new_dirs, changes = merge_dirs(r["directions"] or "")
        if changes and new_dirs != (r["directions"] or ""):
            out.append(
                {
                    "uid": r["uid"],
                    "name": r["name"],
                    "changes": changes,
                    "directions": new_dirs,
                }
            )
    con.close()
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--uid", type=str, default="", help="Only this recipe UID")
    args = ap.parse_args()
    cands = list_candidates()
    if args.uid:
        cands = [c for c in cands if c["uid"] == args.uid]
    OUT.write_text(
        json.dumps(
            [{"uid": c["uid"], "name": c["name"], "changes": c["changes"]} for c in cands],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    safe_print(f"Candidates: {len(cands)} (see {OUT.name})")
    for c in cands:
        safe_print("-", (c["name"] or "")[:58], "|", "; ".join(c["changes"]))

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
                safe_print("FAIL", c["name"])
        await lim.wait_turn()
        async with session.post(
            f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
        ) as r:
            safe_print("notify", r.status)
    safe_print(f"Updated {updated}/{len(cands)}")


if __name__ == "__main__":
    asyncio.run(main())
