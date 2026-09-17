"""
Global Paprika recipe cleanup with exponential backoff on 429s.

  1. Strip leading "Ingredients: …" block from Description
  2. Insert a blank line after each numbered Directions step

Usage:
  python cleanup_desc_and_directions.py --apply

Progress file only records successfully saved (or confirmed-clean) UIDs.
On 429, backs off exponentially with jitter and retries.
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
# Only UIDs we have successfully saved OR confirmed clean after a full fetch
PROGRESS = ROOT / "scripts" / "tools" / ".cleanup_desc_dirs_progress.json"

STEP_LINE = re.compile(r"^\s*\d+[.)]\s+\S", re.M)


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


def clean_description(desc: str) -> str | None:
    """Remove leading Ingredients: brief list. Return new text or None."""
    if not desc:
        return None
    # Strip BOM / odd leading whitespace
    text = desc.replace("\ufeff", "").replace("\u200b", "")
    if not re.match(r"(?i)^\s*ingredients?\s*:", text):
        return None
    # Split on first blank line
    parts = re.split(r"\r?\n[ \t]*\r?\n", text, maxsplit=1)
    if len(parts) == 2:
        rest = parts[1].strip()
        new = (rest + "\n") if rest else ""
    else:
        # Entire description was just the ingredients brief
        new = ""
    return new if new != desc else None


def clean_directions(text: str) -> str | None:
    if not text or not STEP_LINE.search(text):
        return None

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if STEP_LINE.match(line):
            out.append(line.rstrip())
            i += 1
            while i < n and not lines[i].strip():
                i += 1
            if i < n:
                out.append("")
        else:
            out.append(line.rstrip())
            i += 1

    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.rstrip() + "\n"
    original = normalized if normalized.endswith("\n") else normalized + "\n"
    if result == original:
        return None
    return result


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


async def api_json(session, limiter, method, url, *, headers=None, data=None, attempts=14):
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
    # Corrupt progress from earlier runs — start clean unless --resume
    resume = "--resume" in sys.argv
    done: set[str] = set()
    if resume and PROGRESS.exists():
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
        safe_print(f"Resuming; {len(done)} already done")
    elif PROGRESS.exists():
        PROGRESS.unlink()
        safe_print("Cleared stale progress (use --resume to keep it)")

    user, password = paprika_credentials()
    limiter = RateLimiter()
    timeout = aiohttp.ClientTimeout(total=90)

    async with aiohttp.ClientSession(timeout=timeout) as s:
        safe_print("Logging in…")
        token = None
        for attempt in range(12):
            status, body = await api_json(
                s,
                limiter,
                "POST",
                f"{PAPRIKA_API}/v1/account/login",
                data={"email": user, "password": password},
            )
            if (
                status == 200
                and isinstance(body, dict)
                and body.get("result", {}).get("token")
            ):
                token = body["result"]["token"]
                break
            safe_print(f"  login retry {attempt + 1} status={status}")
            await asyncio.sleep(3 * (attempt + 1))
        if not token:
            raise SystemExit("Login failed")
        headers = {"Authorization": f"Bearer {token}"}

        safe_print("Fetching index…")
        index = None
        for attempt in range(12):
            status, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers
            )
            if status == 200 and isinstance(body, dict) and "result" in body:
                index = body["result"]
                break
            safe_print(f"  index retry {attempt + 1} status={status}")
        if not index:
            raise SystemExit("No recipe index")

        safe_print(
            f"Index={len(index)} MODE={'APPLY' if APPLY else 'DRY-RUN'} "
            f"todo≈{len(index) - len(done)}"
        )

        desc_n = dirs_n = saved = failed = confirmed_clean = 0
        t0 = time.monotonic()

        for i, entry in enumerate(index, 1):
            uid = entry["uid"]
            if uid in done:
                if i % 200 == 0:
                    safe_print(f"… {i}/{len(index)} (skipping done) saved={saved}")
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
                safe_print(f"  fetch fail [{i}] status={status}")
                continue

            rec = body["result"]
            # Process trash too (dirty imports often only remain there)

            name = rec.get("name") or ""
            desc = rec.get("description") or ""
            dirs = rec.get("directions") or ""
            ings = rec.get("ingredients") or ""
            if not name:
                failed += 1
                continue
            if not ings and not dirs and not desc:
                failed += 1
                safe_print(f"  empty stub [{i}] {uid[:8]} — will retry next run")
                continue

            new_desc = clean_description(desc)
            new_dirs = clean_directions(dirs)

            if new_desc is None and new_dirs is None:
                confirmed_clean += 1
                done.add(uid)
            else:
                if new_desc is not None:
                    desc_n += 1
                    rec["description"] = new_desc
                if new_dirs is not None:
                    dirs_n += 1
                    rec["directions"] = new_dirs

                if not APPLY:
                    done.add(uid)
                    safe_print(
                        f"WOULD {name} (desc={new_desc is not None}, dirs={new_dirs is not None})"
                    )
                else:
                    rec["hash"] = calc_hash(rec)
                    if await save_recipe(s, limiter, headers, rec):
                        saved += 1
                        done.add(uid)
                        safe_print(
                            f"SAVED [{saved}] {name} "
                            f"(desc={new_desc is not None}, dirs={new_dirs is not None})"
                        )
                    else:
                        failed += 1
                        safe_print(f"FAIL {name}")

            if i % 25 == 0 or i == len(index):
                PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
                elapsed = max(0.1, time.monotonic() - t0)
                safe_print(
                    f"… {i}/{len(index)} desc={desc_n} dirs={dirs_n} "
                    f"saved={saved} clean={confirmed_clean} fail={failed} "
                    f"429s={limiter.hits_429} {(i / elapsed):.2f} items/s"
                )

        if APPLY:
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

        PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
        safe_print(
            f"\nDone. saved={saved} desc_fixed={desc_n} dirs_fixed={dirs_n} "
            f"confirmed_clean={confirmed_clean} failed={failed} 429s={limiter.hits_429}"
        )
        if APPLY and failed == 0:
            PROGRESS.unlink(missing_ok=True)
            safe_print("Progress cleared.")


if __name__ == "__main__":
    asyncio.run(main())
