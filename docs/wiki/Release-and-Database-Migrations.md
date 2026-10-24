# Releases and database migrations

The online upgrade/rollback API, Admin release module and privileged Updater are
retired. Software replacement is an explicit operator maintenance-window action.
External CD belongs to its operator; this product does not install host services.

The repository still requires exact `dev` CI, complete native acceptance, one
same-repository `dev -> main` PR and identical reviewed/main code trees. Publishing
source, deploying software and completing a database migration are separate facts.

## Persistent recovery boundary

Back up every application database table before migration, together with node
identity/vault, runtime secrets, media premaster identity/key and the entire
private ACME account plus published certificate generations. The ordinary
Follower business Backup is a bounded subset and deliberately excludes the
Master premaster; it does not replace a full operator backup.

Stop all writers before taking a cold recovery snapshot, including Web and the
certificate agent. SQLite uses a consistent Connection.backup copy plus a full
SQL dump and actual SQL restoration/table hash verification. MySQL deployments
retain complete cold-volume recovery with the exact recorded image and platform.
Protect snapshots with private permissions and copy verified artifacts to an
independent machine. Media backup is an independent operator decision.

For an S3 deployment, the control snapshot includes S3 configurations, encrypted
credentials, object bindings and unfinished transactions together with the node
vault. It does not contain remote bucket objects. Preserve the matching bucket
data independently and fence writers before restoring control state; an old SQL
snapshot is not permission to overwrite later S3 uploads or remove unknown keys.

Never use `docker compose down -v` for upgrades. Never delete ACME volumes, node
vault, premaster or runtime secrets as cache cleanup. Unix sockets and temporary
Web/cache files are regenerated rather than restored.

## Database generations

Default SQLite is a repository-owned schema contract. Optional original MySQL
remains supported.

The renewal candidate reaches generation 9; these additions do not become a
released schema merely because `dev` is public. The current renewal ledger
records the separate implementation, acceptance and promotion gates.

Existing marked generation 1/2 databases migrate in order:

- generation 3 widens priorities to signed integers in -7..500000 without
  resetting historical values or unrelated business rows;
- generation 4 adds browser encryption descriptors/key identity and the signed
  encrypted-storage upload marker without decrypting media in the backend;
- the renewal's generation 5 adds resource-migration plans,
  tasks, complete-parent units, object proofs, reservations, drains, scopes,
  runner epochs, storage publication journals, read leases and peer bindings.
  It adds tables without rewriting previous media or identity rows. Its runtime
  supplies the complete-parent transfer pipeline described in the migration contract.
- generation 6 adds four independent S3 configuration,
  upload, part and object-binding tables. Existing media, keys, identities and
  generation-5 definitions are unchanged. These tables support the optional
  managed S3 adapter; they do not import unrelated bucket data.
- generation 7 extends the migration task's
  permitted pause/resume phases to publication and delayed cleanup. It does not
  modify old generation signatures or copy media. SQLite replaces the task table
  in one writer transaction with complete row comparisons; unknown task-table
  structures or incoming references refuse the rebuild. MySQL replaces just the
  named check in one native ALTER. An interruption after MySQL DDL keeps the old
  marker and requires idempotent validation/retry before it advances.
- generation 8 adds the independent retirement
  journal. Preparation binds the exact relationship, node identities, keys and
  credential, commits its write fence and audit together, and preserves all
  previous tables. Required checks, column collations, indexes and unknown
  triggers are validated before completion. SQLite rolls back interrupted DDL;
  MySQL retries a committed CREATE and individually missing indexes. Runtime
  preparation, terminal authentication and Admin behavior are described in
  [the retirement contract](../python-node-retirement.md); their acceptance gates
  remain distinct from schema construction.
- generation 9 adds one native cancellation-census
  table containing current resource paths, identities, sizes and stat snapshots.
  It does not copy payloads or change generations 2–8 signatures. Snapshot
  indexes, checks and unknown triggers are verified; interrupted SQLite/MySQL
  upgrades retain their ordinary rollback or idempotent-retry behavior. Current
  Source observation is separate from quota reconciliation and task completion;
  see [the migration contract](../python-resource-migration.md).

Generations 5 and 6 retain prior migration signatures. SQLite DDL and their generation
advance stay in the writer transaction. MySQL DDL may commit before the marker
advances; retries recreate each missing index separately and validate required
columns, types, primary/unique keys and named checks before marking completion.
Merely finding a same-named table is not sufficient. Changing an already
published schema definition requires another generation and signature.

Unknown future schemas, unmarked initialized databases and mismatched migration
history fail closed. Retry an interrupted migration only with the preserved
recovery artifacts and the same reviewed release. A software rollback never
implicitly reverses the database. Restore the full compatible database and
control state if a migration must be reversed.

Equal generation numbers in the Python and Gin repositories do not make their
SQL schemas or node vaults interchangeable. Cross-language conversion requires
its own audited converter and is outside normal restore.

## Operator procedure

The renewal's [Python source operator](../python-source-operator.md) uses a
one-shot host Python process to invoke local Compose. Business containers have
no Docker control permission. Its current entry supports inspection, published
candidate verification, `install`, parameter-free `upgrade`, indexed resume and `recover`.
`upgrade` stages verified published source and executes that version's host entry
under an inherited lock; interrupted work keeps its selected source. Existing
configuration files are retained, and a ready identical release needs no rebuild
or restart. A public release without the versioned host protocol refuses before
fencing. Before fencing, recovery cancels without restarting services;
before database preparation or a completed snapshot, it reuses original resources
and retains incomplete backup material. Later controlled recovery restores and
opens the old package while retaining failed state and post-opening uploads.
Actual Standalone SQLite/MySQL cases cover both paths. The host entry classifies
I/O failures and reports the recorded phase without exposing private inputs.
Staged new-operation upgrades pass local SQLite/MySQL application acceptance,
including the missing init-service addition and new writes after a lost reply.
`resume` and `recover` execute the indexed source too, without choosing a newer
release. Staged SQLite/MySQL recovery preserves incomplete backups, original
resources and post-opening writes across a lost reply. Exact-final-source and
full fixed-role recovery remain release gates. Local native fixture
success does not make an unpublished candidate a deployable release.

The new-install libraries prepare four profiles, pin images and journal owned
empty-resource creation. Canonical files publish without replacement; volume
tokens reconcile lost create replies. The startup library records possible writes
before normal Compose startup, verifies the initialized node and HTTPS read-only,
then publishes the exact installed-package receipt. Lost startup/publication
replies, SQLite/MySQL and Direct/Relay first starts, and a following MySQL upgrade
pass isolated native acceptance. Installed-origin SQLite/MySQL physical recovery
also preserves original identities, media inodes and original-session Range 206,
while retaining failed control roots. It does not infer DDL rollback. Existing
data/conflicts and changed private inputs refuse. The candidate `install` entry
also passes actual source exec, host-exit resumption and same-release upgrade
checks for all four profiles. Storage needs its selector only on first installation;
retry reads the stored role. Same-directory foreign Compose projects refuse before
building and again before resource/startup mutation. The currently published old
source lacks the new install protocol. Follow the source-operator contract for
candidate commands; private-CA/local-source fixtures are not public-release proof.

Repeated upgrades now route a completed managed deployment through its retained
verified source before an old original entry performs live discovery. The same
directory lock survives both source execs. Local application acceptance includes
an added init service followed by a complete second upgrade through a simulated
old discovery limitation. Local source revisions/CA admission are explicit; this
does not substitute for genuine published-source and final-suite release proof.

Follow [the recovery and unarchive guide](../operator-recovery.md) for the actual
backup/restore commands, deployment image selection, maintenance fence, readiness
checks and rollback boundary. Existing external certificates require the fixed
offline import described in [automatic TLS](../automatic-tls-python.md), before
removing retired certificate path variables. New installations request their own
certificates automatically.

Do not reopen business traffic until schema validation, authenticated node
health, trusted HTTPS served-certificate proof and representative media Range
requests pass. Remove only the operator's own maintenance fence after acceptance;
Admin cannot override it.
