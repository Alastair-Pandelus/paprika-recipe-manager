"""Apply db/schema.sql to the local Postgres mirror."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from db.connection import connect, database_url  # noqa: E402


def main() -> None:
    schema = ROOT / "db" / "schema.sql"
    sql = schema.read_text(encoding="utf-8")
    print(f"Applying {schema} → {database_url(redacted=True)}")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print("Schema applied.")


if __name__ == "__main__":
    main()
