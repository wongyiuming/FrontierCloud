# Verified native MySQL Master to SQLite

This is an explicit, offline, second-stage operator migration, not an `.env`
toggle, cluster release, general SQL dump translator or role reset. The running
source must already have genuine native MySQL admission. Keep its store binding,
receipt, data, database volume and immutable runtime images intact.

Hosted CI has a three-minute hard budget. Compile, unit/vet checks, real-driver
conversion and restored-data/runtime acceptance execute on the provided private
development host. A green lightweight CI check alone is not migration authority.

## Preconditions

Stop/fence all application, updater, filesystem and database writers. Hold public
HTTPS on an independent static maintenance responder. Use fresh private SQL,
configuration/secrets, Redis and complete media backups. Independently restore
them and compare `master-migration snapshot` inventories byte-for-byte against
the FINAL stopped source. Earlier public-state snapshots are not final backups.

The converter requires that exact private recovery inventory and its SHA256,
original Master ID, and local root MySQL socket/password secret. It holds the
source native exclusive maintenance lease and global MySQL read lock through
conversion, verification, target admission and durable reporting. It rejects
unfinished business intents, undecryptable credentials, unknown tables/columns,
unsafe roots and nonempty targets. Original expired reservations/history are
retained, not cancelled to make a proof pass.

## Separate target root

Create an EMPTY distinct target data directory on the same filesystem. Bind the
source and target's common host parent once (e.g. `/transfer`), and the target a
second time at its FINAL runtime data path (e.g. `/app/data`). Linux disallows
hardlinks across distinct bind-mount boundaries even on the same underlying
filesystem. `--link-target-root` must resolve to the exact same target directory
inode, through the common-parent mount. Neither alias may overlap source data
or secrets. Use final `/app/data/frontiercloud.db` so the new SQLite binding
matches the existing production runtime's container path.

The operator links all inventoried business files into the new root and preserves
permissions/ownership. Hardlinks save disk space but **are not a backup**: future
in-place writes can affect both links. Keep independently verified off-host media
backups and final deltas. Native provenance/control files are not copied into a
different store; the genuine source receipt is never deleted or rewritten.

## Conversion and proof

The converter creates the shared canonical SQLite schema and inserts EVERY
source row, never textual SQL replacements. It validates column order and
generated-column mapping. Full canonical cell-byte digests include NULLs, binary
payloads, JSON text, timestamp microseconds and generated values. All six legacy
MySQL auto-ID tables use SQLite `AUTOINCREMENT` and retain the source next-ID
floor, including deleted IDs. Unknown schema customization needs separate review;
this migration selects the reviewed shared SQLite schema, not MySQL collation
or physical DDL identity.

`PRAGMA integrity_check` must pass. An independent `VACUUM INTO` SQLite recovery
artifact is opened separately and all row/next-ID digests are compared again.
Every original business file/directory and secret is reverified before the
existing `AdmitVerifiedMaster` API publishes genuine admission in the new root.
Both source and target remain fenced; this command never edits deployment `.env`,
stops MySQL, resumes traffic or starts the production application.

Run the fixed-SHA operator binary with MySQL source configuration:

```text
master-to-sqlite
  --node-id ORIGINAL_MASTER_ID
  --mysql-socket /var/run/mysqld/mysqld.sock
  --manifest /proof/independently-restored-inventory.json
  --proof-sha256 EXACT_SHA256
  --target-data-root /app/data
  --link-target-root /transfer/EMPTY_TARGET
  --target-sqlite-path /app/data/frontiercloud.db
  --sqlite-recovery-file /proof/new-recovered-sqlite.db
  --report /proof/new-transfer-report.json
```

Proof/report paths must be outside both data roots and secrets; the new SQLite
recovery file's directory must be private. Existing completed artifacts are
never overwritten. Rejected partial targets remain fenced and are preserved
for diagnosis; retry with a distinct empty target.

## Runtime acceptance and rollback boundary

Use the SAME tested production Go runtime. Privately validate the original
Admin Key, identity, all active AND historical node relations/credentials,
quotas, priorities, lyrics, users, recordings, security and WebRTC history.
Require real authenticated reachability to both original Direct and Relay peers,
local/remote byte-exact range/full streaming and original recording receipts.
Re-fence after checks and compare all immutable rows/history, files and secrets;
allow only explicitly validated heartbeat/health observations and appended audit
events. Test failures keep public traffic in maintenance and the source intact.

Before public SQLite writes, app-only rollback can resume the untouched current
MySQL source. After public reopening, its frozen MySQL is stale: **do not roll
back by merely switching `.env` or restoring an old SQL/Redis dump**. Recovery
must preserve/reconcile all subsequent SQLite writes. Disable any automatic
watchdog that could resurrect the stale source after public writes.

Only stop (do not delete) original MySQL after acceptance. This naturally releases
its resident and swapped pages. Re-measure available memory, per-process swap,
swap-in/out, CPU, I/O wait and pressure; do not claim language/store migration
alone is a measured performance improvement. Global `drop_caches`/`swapoff` are
not default cleanup operations. Retain Redis, security state and business caches
unless a separately measured and bounded cleanup is demonstrably safe.
