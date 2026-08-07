# Media-library STRM Missing Repair Design

## Goal

CMS `cloud_data` tracks media-library files with `action='STRM'` and `status=1`, meaning a local `.strm` should exist under `/mnt/user/Unraid/strm/转存`. History shows these files can disappear (CMS incremental-sync delete events, manual cleanup) while the database still marks them present, so CMS auto-organize skips rebuilding them (洗版 skip). This change adds a periodic patrol that detects missing media-library STRM files and regenerates them from the CMS direct-link pick_code.

## Scope

In scope:
- Read-only scan of `cms-online.db` `cloud_data` (`action='STRM' AND status=1`).
- Map container path `/media/...` to host `/mnt/user/Unraid/strm/...`.
- Detect missing `.strm` files under the mapped media root.
- Regenerate missing files using the CMS direct domain (`DIRECT_115_302_DOMAIN`) and pick_code:
  `{domain}/d/{pick_code}.mkv?/{name}`.
- Periodic patrol wired into the existing maintenance loop, with an env toggle and interval/limit.
- Unit tests covering scan + repair + host-path mapping.

Out of scope:
- No changes to CMS (PyArmor-obfuscated).
- No repair of `/media/share` self-share STRMs (already covered by existing self-share guard).
- No deletion of files, no 115 API calls.

## Design

1. `CmsCloudDataIndex.missing_media_strm_candidates(host_strm_root, limit)`:
   - Query `SELECT fid, name, pick_code, local_path FROM cloud_data WHERE action='STRM' AND status=1 AND local_path LIKE '/media/%' ORDER BY fid LIMIT ?`.
   - For each row, host path = `host_strm_root / local_path.removeprefix('/media')`, expected strm = dir / name-with-`.strm` suffix.
   - Return rows where the expected strm file is missing.
2. `repair_missing_media_strms(index, host_strm_root, direct_domain, limit, dry_run=False)`:
   - For each candidate, write strm content (only if not dry-run), skip invalid pick_code/domain, return repaired count.
3. Config: `MEDIA_STRM_REPAIR_ENABLED` (default true), `MEDIA_STRM_REPAIR_INTERVAL_SECONDS` (default 21600), `MEDIA_STRM_REPAIR_LIMIT` (default 200).
4. Wire into `start_status_repair_loop` (and self-share maintenance loop) when CMS state db is readable.
5. Log at INFO when repaired; never raise out of the loop.

## Error handling

- Scan/repair exceptions are caught and logged; the patrol never blocks task processing.
- Missing/invalid pick_code or non-direct domain rows are skipped.
- Only `status=1` STRM rows are considered; no deletion ever happens.

## Testing

- Candidate scan finds a missing strm for a known cloud_data row and ignores existing files.
- Repair writes the exact direct-link content and returns the count; dry-run writes nothing.
- Host path mapping handles `/media/转存/...` → `/mnt/user/Unraid/strm/转存/...`.
- Full local unittest suite passes.
