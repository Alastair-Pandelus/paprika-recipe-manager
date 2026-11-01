# Desktop sync stuck / cloud edits reverted

## Symptom

- The Windows app spins on "Syncing" and never finishes (log shows an
  AccountInfoView thread error), and/or
- an edit made through the sync API (`/v2/sync/...`) shows briefly on the cloud,
  then reverts to the old value; the phone keeps showing the old value.

## Cause

A script mirrored rows into the local `Paprika.sqlite` with `status='new'` or
`'modified'` and `is_synced=0`, although those rows already exist on the cloud.
The desktop app treats them as its own pending edits, keeps trying to upload
them, and pushes their (stale) values over newer cloud data.

Seen 2026-10-13 with: 11 meal-plan leftovers in `meals` (`new`), Oatcakes with
Cheese in `recipes` (`modified`), and all 4 rows in `pantry_items` (`modified`,
left by `set_freezer_pantry.py`'s local mirror).

Rebooting or restarting the app does not fix it.

## Fix

1. For each pending local row (`status != 'unmodified'` or `is_synced = 0`)
   whose uid is already on the cloud, mark it synced locally:
   `UPDATE <table> SET status='unmodified', is_synced=1, sync_hash=<cloud hash> WHERE uid=?`
   (`scripts/tools/_clear_stuck_sync_pending.py` does this for meals and one
   recipe; dry-run first, `--apply` to write).
2. Check pending counts are 0 for the affected tables.
3. Fully quit Paprika and reopen it, then sync once.

## Avoid it

- Don't mirror API writes into the local SQLite as `new`/`modified`. Write
  through the API only and let the desktop app pull the change.
- Before an API write, check the desktop app has no pending rows in that table,
  or it will overwrite the cloud on its next sync.
- `scripts/tools/update_pantry_quantities.py` writes through the API only.
