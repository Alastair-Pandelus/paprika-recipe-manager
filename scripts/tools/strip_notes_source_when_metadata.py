"""
Remove duplicate Source lines from Notes when source_url is already set
in Paprika metadata (title area).

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
PROGRESS = ROOT / "scripts" / "tools" / ".strip_notes_source_progress.json"

# Matches a full Source: line (plain URL or Markdown link), plus a following blank line if present
SOURCE_LINE = re.compile(
    r"(?m)^Source:\s*[^\r\n]+(?:\r?\n[ \t]*\r?\n|\r?\n)?",
    re.IGNORECASE,
)


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


def strip_source_from_notes(notes: str) -> str | None:
    if not notes or not SOURCE_LINE.search(notes):
        return None
    new = SOURCE_LINE.sub("", notes, count=1)
    new = re.sub(r"^\s*\n+", "", new)
    new = re.sub(r"\n{3,}", "\n\n", new)
    new = new.strip()
    if new:
        new = new + "\n" if not new.endswith("\n") else new
    else:
        new = ""
    return new if new != notes else None


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


def has_metadata_source(rec: dict) -> bool:
    url = (rec.get("source_url") or "").strip()
    source = (rec.get("source") or "").strip()
    return bool(url) or bool(source)


async def main() -> None:
    done: set[str] = set()
    if PROGRESS.exists() and "--resume" in sys.argv:
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
        safe_print(f"Resuming; {len(done)} already done")
    elif PROGRESS.exists():
        PROGRESS.unlink()
        safe_print("Cleared stale progress")

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

        stripped = skipped_no_meta = skipped_no_line = saved = failed = 0
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

            notes = rec.get("notes") or ""
            if not SOURCE_LINE.search(notes):
                skipped_no_line += 1
                done.add(uid)
                continue
            if not has_metadata_source(rec):
                skipped_no_meta += 1
                done.add(uid)
                continue

            new_notes = strip_source_from_notes(notes)
            if new_notes is None:
                done.add(uid)
                continue

            stripped += 1
            name = rec.get("name") or ""
            safe_print(f"STRIP [{stripped}] {name}")

            if APPLY:
                rec["notes"] = new_notes
                rec["hash"] = calc_hash(rec)
                if await save_recipe(s, limiter, headers, rec):
                    saved += 1
                    done.add(uid)
                    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
                else:
                    failed += 1
                    safe_print(f"  FAIL {name}")
            else:
                done.add(uid)

            if i % 50 == 0:
                safe_print(
                    f"… {i}/{len(index)} stripped={stripped} saved={saved} "
                    f"no_line={skipped_no_line} no_meta={skipped_no_meta} "
                    f"fail={failed} 429s={limiter.hits_429} "
                    f"{time.monotonic() - t0:.0f}s"
                )

        if APPLY and saved:
            await limiter.wait_turn()
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/notify/", headers=headers
            ) as r:
                await r.text()

        safe_print(
            f"\nDone. stripped={stripped} saved={saved} "
            f"kept_no_source_line={skipped_no_line} "
            f"kept_no_metadata={skipped_no_meta} failed={failed} "
            f"429s={limiter.hits_429}"
        )
        if APPLY and failed == 0:
            PROGRESS.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
