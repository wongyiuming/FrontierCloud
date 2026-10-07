# Verified legacy Follower to Gin + SQLite

This operator-only workflow is not an .env toggle or an ordinary release.
It preserves the original Follower identity, encrypted key, upstream credential,
active/revoked relationships, business rows, media IDs and all physical data.
It never promotes to Standalone/Master or pairs a replacement node.

Keep original MySQL, data, secrets, images and exact Compose configuration.
Precopy media off-host while online, then fence all legacy Web/Updater and
filesystem writers. Take final SQL/config/secret/Redis backups and synchronize
the stopped data delta. Restore the final backup independently in an isolated
fixture; a live precopy or a hardlink tree is NOT recovery proof.

`follower-migration snapshot` hashes every row, canonical DDL, physical file,
mode and secret under the authoritative MySQL global read lock. It requires
the exact original Follower ID and schema generation 2. Its separate
frontiercloud-offline-mysql-follower-v1 format cannot be substituted with a
Master inventory. Snapshotting cannot grant admission in legacy MySQL.

`follower-to-sqlite` uses the existing exhaustive MySQL-to-SQLite transfer:
full row/NULL/binary/timestamp/generated-value/next-ID digests, SQLite integrity,
an independent SQLite recovery file, and unchanged file/secrets proof. Source
and restored inventories must match exactly; unknown schema and unfinished
intents are rejected, not silently removed. Target identity/role must match.
Only then can the Follower-specific offline admission boundary publish a
native receipt in the new store. Both roots remain in maintenance.

Legacy counters can retain reservations for uploads that never had a durable
local SQL session. That is not permission to clear them blindly. First compare
the Master's publication/upload inventory, exact physical staging bytes and
the Follower's local intent tables. Preserve the original SQL and staging file
off-host; fence writers and recheck. A required accounting-only recovery must
use exact identity/counter conditions in a transaction, leave original quota
and usage untouched, and append an audit with old/new values and proof hashes.
Keep abandoned staging bytes in a private quarantine, never delete them merely
to make conversion succeed. The converter itself still rejects inconsistency.

Recordings need the existing Master's signed offline inventory, not fabricated
Follower rows. Export only during an authorized short Master maintenance window,
with independent bounded resumption on disconnect. Resume the Master immediately;
its signature expires after 30 minutes. Adopt media receipts first, then recordings,
under the closed Follower lease with exact signature, bytes, hashes and usage checks.

`adopt-storage --wait-seconds` includes the complete physical hash scan. Its
default remains 300 seconds; for a large existing disk on a small CPU, explicitly
allow up to 1800 seconds. The maintenance lease remains held throughout; this
does not extend public HTTP deadlines or relax identity/accounting/file checks.
Master recording export and ordinary drain limits are unchanged.

MySQL's verified SHOW CREATE TABLE issue #110825 can make an independently
restored dump print redundant CHARACTER SET clauses after implicit COLLATE
becomes explicit. Keep the raw dump intact. Reproduce the original implicit
column metadata only in the isolated fixture, retaining every explicit charset
and non-default collation. Require the resulting actual DDL/row/file/secret
inventory to match the frozen source exactly; never normalize away a failed proof.

Flags match `master-to-sqlite` documented in offline-master-to-sqlite.md:
manifest, pinned proof SHA256, exact node ID, root MySQL socket, empty distinct
target data root, same-parent hardlink alias, final container SQLite path,
outside-root report and independent SQLite recovery file. The original Master
command still requires an already admitted native Master; no guard is relaxed.

Before reopening, privately verify media storage/recording adoption receipts,
AdminKey, upstream authentication, heartbeat/mode/quota, backup inventory and
byte-exact reads of every original media object. Any required new native
accounting receipt is a separately audited adoption step after row conversion,
not an invented object ID or a refund of original usage. Preserve all source
rows and account for such additions explicitly.

After public SQLite writes begin, returning to frozen MySQL would lose new
state: stop/fence and reconcile before rollback. Never use `compose down -v`,
global prune, a database reset or directory deletion for this migration.
