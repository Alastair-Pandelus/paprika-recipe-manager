"""Copy the local Paprika 1.x Data folder into backups/paprika-local/<date>/."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import BACKUPS_DIR, DEFAULT_LOCAL_DATA  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_LOCAL_DATA,
        help="Paprika Data folder (default: Windows 1.x path)",
    )
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Backup folder date stamp",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        raise SystemExit(f"Source not found: {args.source}")

    dest = BACKUPS_DIR / "paprika-local" / args.date
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(args.source, dest)

    files = list(dest.rglob("*"))
    file_count = sum(1 for p in files if p.is_file())
    total_bytes = sum(p.stat().st_size for p in files if p.is_file())
    manifest = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "source": str(args.source),
        "destination": str(dest),
        "file_count": file_count,
        "bytes": total_bytes,
        "note": "Close Paprika before restoring this folder over the live Data directory.",
    }
    (dest / "BACKUP_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
