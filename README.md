# Paprika Recipe Manager

Versioned workspace for **Paprika Recipe Manager** backups and **Field Doctor Low FODMAP** reverse-engineering scripts.

## What's in here

| Path | Purpose |
| --- | --- |
| `backups/paprika-local/<date>/` | Copy of Windows Paprika **1.x** `Data` folder (`Paprika.sqlite` + photos) |
| `backups/paprika-json/<date>/` | Full cloud API dump (all recipes + categories as JSON) |
| `db/schema.sql` | Postgres mirror schema (Paprika cloud = master) |
| `docker-compose.yml` | Local Postgres for the mirror |
| `scripts/db/` | `init_db.py` + hash-based `sync_from_paprika.py` |
| `scripts/field_doctor/` | Main Field Doctor import / rescale / photo / cupboard tools |
| `scripts/archive/` | Earlier one-off scripts preserved as-is |
| `scripts/tools/` | Backup helpers |
| `docs/` | Cupboard-staple scan outputs + Postgres mirror notes |

## Credentials

Do **not** commit passwords.

1. Copy `.env.example` → `.env` and set `PAPRIKA_USERNAME` / `PAPRIKA_PASSWORD`, **or**
2. Keep using `%USERPROFILE%\.paprika-mcp.env` (already used by the Cursor Paprika MCP).

## Setup

```powershell
cd "C:\dev\Github\Paprika Recipe Manager"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Or use the existing paprika-mcp pipx Python (has `aiohttp` + `Pillow`).

## Backups

### Local Paprika 1.2.4 database

Paprika 1.x has no bulk **File → Export**. Snapshot the data folder instead:

```powershell
# Prefer closing Paprika first
python scripts\tools\backup_local_data.py
```

Source (Windows 1.x): `%LOCALAPPDATA%\Paprika\Data\`

To restore: close Paprika, replace that `Data` folder with a dated copy from `backups/paprika-local/`.

### Cloud JSON snapshot

```powershell
python scripts\tools\backup_cloud_json.py
```

Writes:

- `manifest.json` — counts
- `categories.json`
- `recipes_index.json`
- `recipes_all.json`
- `recipes/*.json` — one file per recipe

## Field Doctor scripts

Working copies live in `scripts/field_doctor/`. Many still hardcode older Temp paths in places — prefer editing those modules in this repo going forward.

High-value entry points:

- `rescale_density.py` — meal rescale / formatting pipeline
- `import_nonmeals.py` / `finish_nonmeals.py` / `fix_photos_nonmeals.py` — porridge + bars
- `cupboard_scan.py` — pantry staples across recipes
- `find_dupes.py` / `dupes_verdict.py` — manual vs Style duplicates

Recipes are home-cooking recreations inspired by Field Doctor products, not official Field Doctor recipes.

## Local Postgres mirror

Paprika cloud stays the master. Sync a queryable copy into Docker Postgres (hash-based; Paprika has no last-touched date):

```powershell
docker compose up -d
pip install -r requirements.txt
python scripts\db\init_db.py          # if schema was not applied on first boot
python scripts\db\sync_from_paprika.py
```

Details: `docs/postgres-mirror.md`

## GitHub

Private repo: https://github.com/Alastair-Pandelus/paprika-recipe-manager

```powershell
cd "C:\dev\Github\Paprika Recipe Manager"
git add .
git status   # confirm .env is NOT listed
git commit -m "Your message"
git push
```
