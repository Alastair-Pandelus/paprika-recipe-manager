"""
Normalize ingredient units for Paprika scaling:
  1. Remove secondary parenthetical units (e.g. cups/tbsp next to primary g/ml)
     so only the scalable primary quantity remains.
  2. Prefer short unit forms: grams/gram -> g (and similar common expansions).

Uses exponential backoff on HTTP 429.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from lib import PAPRIKA_API, paprika_credentials  # noqa: E402

APPLY = "--apply" in sys.argv
PROGRESS = ROOT / "scripts" / "tools" / ".normalize_ingredient_units_progress.json"

# Parenthetical that looks like an alternate measurement
ALT_UNIT_WORDS = (
    r"cups?|tablespoons?|tbsps?|teaspoons?|tsps?|"
    r"millilit(?:er|re)s?|lit(?:er|re)s?|"
    r"\bml\b|\bl\b|"
    r"fl\.?\s*oz|fluid\s*ounces?|"
    r"\bounces?\b|\boz\b|"
    r"\bpounds?\b|\blbs?\b|"
    r"\bkg\b|kilograms?|"
    r"\bgrams?\b|\bg\b|"
    r"pints?|quarts?|gallons?|"
    r"cloves?|\bcans?\b|sticks?|slices?|pieces?|"
    r"inches?|\bcm\b|\bmm\b|"
    r"handfuls?|pinches?|dashes?"
)

# e.g. "100 g (1/2 cup)", "2 tbsp (30 ml)", "1 cup (240 ml)"
PAREN_ALT = re.compile(
    rf"\s*\((?=[^)]*(?:{ALT_UNIT_WORDS}))[^)]*\)",
    re.IGNORECASE,
)

# "250 g / 1 cup" or "100 g/3.5 oz" — slash alternate after a primary unit
SLASH_ALT = re.compile(
    rf"\b(g|kg|ml|oz|lb|tbsps?|tsps?|cups?)\s*/\s*"
    rf"(?:\d+(?:[./]\d+)?|[½¼¾⅓⅔]|[\d./½¼¾⅓⅔]+)\s*(?:{ALT_UNIT_WORDS})",
    re.IGNORECASE,
)

# Unit word normalizations (word-boundary aware)
UNIT_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgrams?\b", re.I), "g"),
    (re.compile(r"\bkilograms?\b", re.I), "kg"),
    (re.compile(r"\bmillilit(?:er|re)s?\b", re.I), "ml"),
    (re.compile(r"\blit(?:er|re)s?\b", re.I), "l"),
    (re.compile(r"\btablespoons?\b", re.I), "tbsp"),
    (re.compile(r"\bteaspoons?\b", re.I), "tsp"),
    (re.compile(r"\bounces?\b", re.I), "oz"),
    (re.compile(r"\bpounds?\b", re.I), "lb"),
]


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


def normalize_line(line: str) -> str:
    s = line
    # Don't touch pure section headers like "Sauce:"
    if re.fullmatch(r"\s*[^:\n]+:\s*", s):
        return s

    s = PAREN_ALT.sub("", s)
    s = SLASH_ALT.sub(r"\1", s)

    for pat, repl in UNIT_REPLACEMENTS:
        s = pat.sub(repl, s)

    # tidy spaces left by removals: "100 g  flour" -> "100 g flour"
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+,", ",", s)
    return s.rstrip()


def normalize_ingredients(text: str) -> str | None:
    if not text:
        return None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    new_lines = [normalize_line(ln) for ln in lines]
    new = "\n".join(new_lines)
    # preserve trailing newline style
    if text.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    return new if new != text else None


class RateLimiter:
    def __init__(self, min_interval: float = 0.7, max_backoff: float = 180.0) -> None:
        self.min_interval = min_interval
        self.max_backoff = max_backoff
        self._lock = asyncio.Lock()
        self._next_ok = 0.0
        self._backoff = 0.0
        self.hits_429 = 0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_ok - now)
            if delay:
                await asyncio.sleep(delay)
            self._next_ok = time.monotonic() + self.min_interval

    async def penalize(self) -> None:
        async with self._lock:
            self.hits_429 += 1
            self._backoff = 3.0 if self._backoff <= 0 else min(
                self.max_backoff, self._backoff * 2
            )
            wait = self._backoff + random.uniform(0, self._backoff * 0.3)
            self._next_ok = time.monotonic() + wait
            safe_print(f"  429 → sleep {wait:.1f}s (total 429s={self.hits_429})")
        await asyncio.sleep(wait)

    def reward(self) -> None:
        if self._backoff > 0:
            self._backoff = max(0.0, self._backoff * 0.5)
            if self._backoff < 1.5:
                self._backoff = 0.0


async def api_json(session, limiter, method, url, *, headers=None, data=None, attempts=12):
    last_status, last_body = 0, None
    for attempt in range(attempts):
        await limiter.wait_turn()
        try:
            async with session.request(method, url, headers=headers, data=data) as resp:
                last_status = resp.status
                if resp.status == 429:
                    await limiter.penalize()
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(min(40, 2 * (attempt + 1)))
                    continue
                try:
                    last_body = await resp.json(content_type=None)
                except Exception:
                    last_body = await resp.text()
                if resp.status == 200:
                    limiter.reward()
                return resp.status, last_body
        except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
            safe_print(f"  net err: {ex}")
            await asyncio.sleep(min(40, 2 * (attempt + 1)))
    return last_status, last_body


async def save_recipe(session, limiter, headers, rec: dict) -> bool:
    payload = gzip_obj(rec)
    for attempt in range(12):
        form = aiohttp.FormData()
        form.add_field(
            "data", payload, content_type="application/octet-stream", filename="data"
        )
        await limiter.wait_turn()
        try:
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{rec['uid']}/",
                headers=headers,
                data=form,
            ) as resp:
                if resp.status == 429:
                    await limiter.penalize()
                    continue
                text = await resp.text()
                ok = '"result":true' in text.replace(" ", "")
                if ok:
                    limiter.reward()
                return ok
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(min(40, 2 * (attempt + 1)))
    return False


async def main() -> None:
    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
        safe_print(f"Resuming; {len(done)} done")
    elif PROGRESS.exists():
        PROGRESS.unlink()
        safe_print("Cleared stale progress")

    # Quick self-check
    demos = [
        "100 g (1/2 cup) flour",
        "2 tablespoons (30 ml) oil",
        "200 grams sugar",
        "1 cup (240 ml) milk",
        "Sauce:",
        "1 handful fresh parsley",
    ]
    safe_print("Normalize demos:")
    for d in demos:
        safe_print(f"  {d!r} -> {normalize_line(d)!r}")

    user, password = paprika_credentials()
    limiter = RateLimiter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        safe_print("Logging in…")
        status, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        )
        if status != 200:
            raise SystemExit(f"login failed: {body}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        status, body = await api_json(
            s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers
        )
        index = body["result"]
        safe_print(f"Index={len(index)} MODE={'APPLY' if APPLY else 'DRY-RUN'}")

        changed = saved = failed = unchanged = 0
        t0 = time.monotonic()

        for i, entry in enumerate(index, 1):
            uid = entry["uid"]
            if uid in done:
                continue

            status, body = await api_json(
                s,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            if status != 200 or not isinstance(body, dict) or not body.get("result"):
                failed += 1
                continue
            rec = body["result"]
            if rec.get("in_trash"):
                done.add(uid)
                continue

            ings = rec.get("ingredients") or ""
            new_ings = normalize_ingredients(ings)
            if new_ings is None:
                unchanged += 1
                done.add(uid)
            else:
                changed += 1
                name = rec.get("name") or ""
                # show a short before/after sample line
                old_lines = [ln for ln in ings.splitlines() if ln.strip()]
                new_lines = [ln for ln in new_ings.splitlines() if ln.strip()]
                sample = ""
                for a, b in zip(old_lines, new_lines):
                    if a != b:
                        sample = f"{a}  =>  {b}"
                        break
                safe_print(f"FIX [{changed}] {name}")
                if sample:
                    safe_print(f"  {sample}")

                if APPLY:
                    rec["ingredients"] = new_ings
                    rec["hash"] = calc_hash(rec)
                    if await save_recipe(s, limiter, headers, rec):
                        saved += 1
                        done.add(uid)
                        PROGRESS.write_text(
                            json.dumps(sorted(done)), encoding="utf-8"
                        )
                    else:
                        failed += 1
                        safe_print(f"  FAIL {name}")
                else:
                    done.add(uid)

            if i % 50 == 0:
                safe_print(
                    f"… {i}/{len(index)} changed={changed} saved={saved} "
                    f"unchanged={unchanged} fail={failed} 429s={limiter.hits_429} "
                    f"{time.monotonic() - t0:.0f}s"
                )

        if APPLY and saved:
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

        safe_print(
            f"\nDone. changed={changed} saved={saved} unchanged={unchanged} "
            f"failed={failed} 429s={limiter.hits_429}"
        )
        if APPLY and failed == 0:
            PROGRESS.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
