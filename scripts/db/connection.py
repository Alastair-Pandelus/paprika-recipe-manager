"""Postgres connection helpers for the Paprika local mirror."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import load_env  # noqa: E402

DEFAULT_DATABASE_URL = "postgresql://paprika:paprika@localhost:5432/paprika"


def database_url(*, redacted: bool = False) -> str:
    load_env()
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    if not redacted:
        return url
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def connect():
    try:
        import psycopg
    except ImportError as ex:
        raise SystemExit(
            "psycopg is required. Run: pip install 'psycopg[binary]>=3.1'"
        ) from ex
    return psycopg.connect(database_url())
