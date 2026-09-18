"""Upsert Monash-aligned FODMAP green-serve reference data into local SQLite.

  python scripts/tools/upsert_monash_fodmap.py
  python scripts/tools/upsert_monash_fodmap.py --seed data/monash_fodmap_seed.json
  python scripts/tools/upsert_monash_fodmap.py --food '{"id":"carrot",...}'

The official Monash app database is proprietary — we cannot scrape it.
Maintain this table by checking the app and upserting changed rows.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "monash_fodmap.sqlite"
DEFAULT_SEED = ROOT / "data" / "monash_fodmap_seed.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS foods (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  fodmap_type TEXT,
  green_g REAL,
  amber_g REAL,
  red_g REAL,
  no_upper_limit INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  source TEXT,
  source_url TEXT,
  verified_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS food_match_keys (
  food_id TEXT NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
  match_key TEXT NOT NULL,
  PRIMARY KEY (food_id, match_key)
);

CREATE INDEX IF NOT EXISTS idx_food_match_key ON food_match_keys(match_key);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def upsert_food(con: sqlite3.Connection, row: dict) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    food_id = row["id"]
    con.execute(
        """
        INSERT INTO foods (
          id, name, fodmap_type, green_g, amber_g, red_g, no_upper_limit,
          notes, source, source_url, verified_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name,
          fodmap_type=excluded.fodmap_type,
          green_g=excluded.green_g,
          amber_g=excluded.amber_g,
          red_g=excluded.red_g,
          no_upper_limit=excluded.no_upper_limit,
          notes=excluded.notes,
          source=excluded.source,
          source_url=excluded.source_url,
          verified_at=excluded.verified_at,
          updated_at=excluded.updated_at
        """,
        (
            food_id,
            row["name"],
            row.get("fodmap_type"),
            row.get("green_g"),
            row.get("amber_g"),
            row.get("red_g"),
            1 if row.get("no_upper_limit") else 0,
            row.get("notes"),
            row.get("source"),
            row.get("source_url"),
            row.get("verified_at"),
            now,
        ),
    )
    con.execute("DELETE FROM food_match_keys WHERE food_id=?", (food_id,))
    for key in row.get("match_keys") or []:
        key = str(key).strip().lower()
        if not key:
            continue
        con.execute(
            "INSERT INTO food_match_keys (food_id, match_key) VALUES (?, ?)",
            (food_id, key),
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    ap.add_argument(
        "--food",
        help="Single food JSON object to upsert (overrides --seed for that one row)",
    )
    args = ap.parse_args()

    if args.food:
        rows = [json.loads(args.food)]
    else:
        rows = json.loads(args.seed.read_text(encoding="utf-8"))
        if isinstance(rows, dict) and "foods" in rows:
            rows = rows["foods"]

    con = connect(args.db)
    for row in rows:
        upsert_food(con, row)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    free = con.execute(
        "SELECT COUNT(*) FROM foods WHERE no_upper_limit=1"
    ).fetchone()[0]
    con.close()
    print(f"Upserted {len(rows)} food(s) → {args.db}")
    print(f"DB totals: {n} foods ({free} no_upper_limit / free)")


if __name__ == "__main__":
    main()
