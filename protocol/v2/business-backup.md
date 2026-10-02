# Existing v2 cold business artifact

This is the compatibility JSONL artifact sent by the native Master over the
[backup transfer](backup-transfer.md). It documents an existing format, not a
new database schema or a completed portable restore workflow. A verified
checksum proves transport integrity, not ownership or safe recoverability.

## Records

The file is UTF-8, one JSON object per line, without compression. It begins with
`{"kind":"header","version":2,"generation":G}` and ends with
`{"kind":"end"}`. `G` is the exact positive 64-bit integer also used in transfer.
An incomplete export never receives the end record or becomes a sendable artifact.

Business records have `kind="row"`, `table` and a `value` object containing that
table's logical column names. The fixed compatibility table set is:

```text
media_visibility             media_objects
media_playback_stats         media_playback_events
media_lyric_links             global_media_objects
cluster_storage_members      cluster_compute_members
cluster_worker_jobs          cluster_backup_members
karaoke_users                karaoke_recordings
karaoke_registration_daily   karaoke_audit_log
admin_audit_log              webrtc_observation_events
webrtc_observation_summary   ip_security_audit_log
ip_security_summary          ip_security_projection
ip_auto_ban_events           ip_permanent_whitelist
node_audit
```

Empty tables produce no row records. No raw SQL, driver objects, physical schema
DDL, database connection settings, node private keys, pairing credentials or
cold backup contents are included. Row numbers preserve exact integer values;
non-finite floats and invalid UTF-8 are rejected. Native datetime columns use
`{"$datetime":"YYYY-MM-DDTHH:MM:SS[.ffffff]"}` in UTC, independent of the selected
driver. Older SQLite artifacts may contain plain datetime strings. Binary SQL
values use `{"$bytes":"standard-base64"}`. Textual JSON column values remain
text; they are not silently converted into a different value type.

Lyric records have `kind="lyric"`, a slash-relative `name` below `media/lyrics`,
and standard-base64 `payload`. Native copies regular `.lrc` files in bounded
directory batches and at most 2 MiB per file. Private dot files and symlinks are
not archived; encountering an unsafe visible symlink fails the export. Media
and recording audio/video bytes are not in this logical artifact.

Native export and preflight bound each complete JSONL record, including its
newline, to 16 MiB. Text cells are at most 4 MiB, and preflight JSON nesting is
bounded to 32. Larger legacy artifacts require explicit migration; they are not
silently truncated or accepted as safe recovery input.

## Native consistency and scheduling

All business tables are read from one repeatable database snapshot. Lyric bytes
are pinned by the shared media mutation lease across processes: ordinary reads
continue, while physical publication, rename and deletion wait. The lease is
released before any backup network request. Private temporary artifacts are
0600 inside a 0700 cache on POSIX hosts and removed after ordinary success,
cancellation or failure; interrupted-process cache maintenance is still a gate.

The compatibility artifact has no replay journals. Native export therefore
defers if its snapshot contains unfinished upload/delete/rename intents,
non-active global placements, non-ready recordings, account deletion or reserved
storage capacity. It never omits that intent while presenting the related rows
as a complete recoverable snapshot. Fully cleaned native rename history may
remain outside the artifact.

One OS-leased serial worker bounds concurrent database scans. It runs separately
from heartbeat probes and uses a separate verified-TLS connection pool. Daily
successful backups and five-minute failed-attempt cooldowns preserve the existing
scheduling intervals. Transfers have a deadline; each message rechecks the
current active downstream, pinned placement and backup enablement. A revoked or
rebound relationship cannot receive a subsequent commit.

The Master advances its verified-generation pointer and success audit only
after an authenticated ready receipt with the exact byte count. Lost begin/chunk
acknowledgements trigger bounded abort cleanup. A lost commit acknowledgement
does not delete the peer's already-ready generation or falsely advance the
Master's pointer; a later attempt uses a new generation.

## Remaining recovery gates

Restoration must validate schema/records, capacity, identity mapping and physical
ownership under a maintenance fence. In particular, missing local media cannot
be invented from these metadata rows, and an old relationship identifier is not
authorization to read another node. Safe restore/promotion, legacy ownership
adoption, crash-cache cleanup and actual mixed-runtime backup interoperability
are not established merely by producing or receiving this artifact.

## Native read-only preflight

`frontiercloud verify-backup` can inspect a file with an explicit generation and
expected SHA-256, or an exact historical Master/generation in the configured
cold backup store. The latter uses one read-only repeatable SQLite/MySQL
snapshot, including during concurrent two-generation retention. It independently
checks ready state, exact contiguous chunks, byte totals and checksum at EOF.
Even an out-of-band oversized SQL BLOB is bounded before driver allocation.
Partial reads, ignored stream errors, missing chunks and corrupt manifests never
produce a successful result. No public or internal HTTP read endpoint is added;
historical local inspection does not require a current pairing credential.

Examples (the generation, checksum and IDs must be the operator's actual values):

```sh
frontiercloud verify-backup --file /private/business.jsonl \
  --generation 1790000000000000123 --sha256 EXPECTED_SHA256 \
  --scratch-dir /private/backup-checks
frontiercloud verify-backup --master-id HISTORICAL_MASTER_ID \
  --generation 1790000000000000123 --scratch-dir /private/backup-checks
```

Preflight never initializes, restores or promotes the authoritative database.
It creates an isolated native SQLite scratch database in an exact random private
child of the specified scratch directory, using the embedded shared schema.
Cross-row state is disk-backed rather than a payload-sized in-memory map; row
values use parameters and table names come only from the fixed v2 allowlist.
Ordinary success, rejection and cancellation remove that child's database and
sidecars. It does not chmod or recursively delete the supplied parent directory.
POSIX child directories/files are 0700/0600. The directory must not be a symlink.
Interrupted-process scratch cleanup is still part of the maintenance gate.

Validation includes exact header generation/version and footer/EOF; UTF-8,
duplicate keys at every JSON level, table/column allowlists, required/null/type
checks, exact signed 64-bit integers, portable microsecond datetime values,
varchar lengths, textual JSON validity, unique keys and shared SQL constraints.
Generated ban columns are recomputed and compared, never directly inserted.
Ownership/reference IDs and path locators are checked; lyric names cannot escape
the root or target private paths, and duplicate/oversized/malformed payloads fail.
The existing plain SQLite datetime and wrapped UTC datetime representations are
accepted without a runtime-name branch.

Logical checks cover media/statistics/lyric identity consistency, lyric payload
presence, placement owners, recording owners/users, exact personal recording
accounting and storage usage lower bounds. Storage may also contain uncatalogued
physical bytes: preflight does not fabricate accounting equality or refund them.
Reserved capacity, non-active placements, non-ready recordings and deleting
accounts are rejected. Username normalization and fixed-cost password-hash
encoding are checked without running scrypt on untrusted input. Audit/history
references are not incorrectly treated as live ownership links.

Only after all integrity and logical checks succeed is a count-only report
emitted, with `logical_valid=true` and always `restore_ready=false`. The report
contains no row contents, passwords, credentials or private identities. Explicit
remaining gates are maintenance fencing, identity/relationship remapping,
physical ownership proofs, and atomic publication/rollback. A caller-supplied
checksum proves byte integrity, not authenticity. Empty tables have no records
in v2, so preflight cannot independently prove a table's historical completeness.
Safe restore must not infer authority from old relationship IDs or reconstruct
omitted upload/ownership journals without authenticated physical proofs.

The scoped native stop/drain prerequisite is documented in
`native-maintenance.md`. Its local lifecycle proof does not establish legacy or
remote writer isolation and does not change preflight's `restore_ready=false`.
