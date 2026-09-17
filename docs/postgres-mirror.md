# Local Postgres mirror (Paprika = master)

Paprika cloud remains the source of truth. This Postgres instance is a **read/query cache** so scripts can search and filter without hitting API rate limits.

## Does Paprika expose a last-touched date?

**No.** Recipe objects only have:

| Field | Meaning |
| --- | --- |
| `hash` | Content fingerprint — **use this for sync** |
| `created` | Timestamp string, but often rewritten on edit (unreliable as updated_at) |

Optimisation path Paprika actually supports:

1. `GET /v2/sync/status/` — incrementing counters (cheap “anything changed?” gate)
2. `GET /v2/sync/recipes/` — `{uid, hash}` list (no full bodies)
3. `GET /v2/sync/recipe/{uid}/` — fetch **only** rows whose hash differs

## Start Postgres (Docker)

```powershell
cd "C:\dev\Github\Paprika Recipe Manager"
docker compose up -d
```

Default connection (also in `.env.example`):

```
postgresql://paprika:paprika@localhost:5432/paprika
```

Schema is applied on first container init. If you need to re-apply:

```powershell
pip install -r requirements.txt
python scripts\db\init_db.py
```

## Sync from Paprika

```powershell
python scripts\db\sync_from_paprika.py
python scripts\db\sync_from_paprika.py --force   # re-diff hashes even if status counters match
```

First run fetches every recipe once (rate-limited). Later runs only pull changed hashes.

## Tables

- `categories` / `recipes` / `recipe_categories` — normalised mirror
- `recipe_index` — local `{uid, hash}` cache for fast diffs
- `sync_state` — last seen status counters
- `recipes.raw_json` — full Paprika payload for lossless fields
