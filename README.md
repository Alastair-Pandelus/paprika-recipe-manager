# Parika Recipe Manager

Versioned workspace for **Paprika Recipe Manager** backups and **Field Doctor Low FODMAP** reverse-engineering scripts.

> Folder name keeps your existing path (`Parika`). Paprika product spelling is still *Paprika*.

## What's in here

| Path | Purpose |
| --- | --- |
| `backups/paprika-local/<date>/` | Copy of Windows Paprika **1.x** `Data` folder (`Paprika.sqlite` + photos) |
| `backups/paprika-json/<date>/` | Full cloud API dump (all recipes + categories as JSON) |
| `scripts/field_doctor/` | Main Field Doctor import / rescale / photo / cupboard tools |
| `scripts/archive/` | Earlier one-off scripts preserved as-is |
| `scripts/tools/` | Backup helpers |
| `docs/` | Cupboard-staple scan outputs |

## Credentials

Do **not** commit passwords.

1. Copy `.env.example` → `.env` and set `PAPRIKA_USERNAME` / `PAPRIKA_PASSWORD`, **or**
2. Keep using `%USERPROFILE%\.paprika-mcp.env` (already used by the Cursor Paprika MCP).

## Setup

```powershell
cd "C:\dev\Github\Parika Recipe Manager"
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

## GitHub

```powershell
cd "C:\dev\Github\Parika Recipe Manager"
git init
git add .
git status   # confirm .env is NOT listed
git commit -m "Initial Paprika backups and Field Doctor tooling"
```

Then create a GitHub repo and push. Current local + JSON backups are ~tens of MB (fine for GitHub without LFS).
