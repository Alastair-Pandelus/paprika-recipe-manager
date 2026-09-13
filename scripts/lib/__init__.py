"""Shared paths and Paprika auth helpers for this project."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
BACKUPS_DIR = PROJECT_ROOT / "backups"
DOCS_DIR = PROJECT_ROOT / "docs"

PAPRIKA_API = "https://paprikaapp.com/api"

# Category UIDs used for Field Doctor Low FODMAP imports
FIELD_DOCTOR_CATEGORY_UID = "E7F559EA-9C6A-4BD9-8114-C6B683F1F922"
LOW_FODMAP_CATEGORY_UID = "a1747733-a5ed-47b8-8d7d-db33fcb19a10"

# Local Paprika 1.x database (Windows)
DEFAULT_LOCAL_DATA = Path.home() / "AppData" / "Local" / "Paprika" / "Data"


def load_env() -> None:
    """Load PAPRIKA_USERNAME / PAPRIKA_PASSWORD from project .env or ~/.paprika-mcp.env."""
    candidates = [
        PROJECT_ROOT / ".env",
        Path.home() / ".paprika-mcp.env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return
    raise FileNotFoundError(
        "No credentials file found. Create .env in the project root "
        "(see .env.example) or keep ~/.paprika-mcp.env."
    )


def paprika_credentials() -> tuple[str, str]:
    load_env()
    user = os.environ.get("PAPRIKA_USERNAME")
    password = os.environ.get("PAPRIKA_PASSWORD")
    if not user or not password:
        raise RuntimeError("PAPRIKA_USERNAME and PAPRIKA_PASSWORD must be set.")
    return user, password
