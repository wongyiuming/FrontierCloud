# Operator recovery and unarchival

The final runtime remains Python/FastAPI. Archival changes GitHub write access;
local clones, running containers, release bundles and frozen images remain
usable. Preserve the final source/tree identity, historical Git bundle, clean
image export, dependency resolution, checksums and sanitized acceptance results
independently of GitHub. Test CA images and fixture secrets are not release
artifacts. Private deployment backups must never become public release assets.

## Full database and control backup

`scripts/operator_backup.py` is an offline operator tool for the local Linux
Docker host. Run it as root. It resolves actual Compose bind/named-volume roots,
rejects foreign writers, stops the selected project, and leaves services stopped.
No product Web container obtains Docker or host service permissions.

Set a hard maintenance fence in the actual deployment data directory first:

```bash
umask 077
touch /absolute/deployment/data/.frontiercloud-native-maintenance
mkdir -p /absolute/private-backups
python3 scripts/operator_backup.py backup \
  --project-directory /absolute/deployment \
  --compose-file /absolute/deployment/docker-compose.yaml \
  --output /absolute/private-backups/before-migration
python3 scripts/operator_backup.py verify \
  --snapshot /absolute/private-backups/before-migration
```

Paths are placeholders. Repeat `--compose-file` for every active overlay,
including MySQL or frozen image selection. Storage uses its storage Compose
file. Keep the actual original project name and persistent volume identities;
do not accidentally initialize a second empty deployment.

`SQLITE_PATH` must name a regular file under the actual persistent `data` root:
`/app/data` inside the product container, or the checkout's `data` directory when
running source tools. An arbitrary absolute path is insufficient. Paths under
`media`, `recordings`, `tmp` or `.tmp`, parent traversal, symbolic links and hard
links are rejected. Keep the container's `/app/data` bind mounted to the intended
`DATA_DIRECTORY`; the default database is `frontiercloud.db` in that root.

Snapshots use 0700 directories and 0600 files. SQLite includes a consistent
binary backup, full SQL dump, integrity/foreign-key checks and actual SQL
restoration with every table's schema, row count and typed content hash checked.
MySQL uses a complete cold-volume snapshot restored with the exact recorded
image and platform; this is not a portable cross-version SQL import.

Snapshot completion is published only after artifact files, the snapshot
directory and its parent's new directory entry have been synchronized to disk.
The final private `manifest.json` is the completion marker. An interrupted or
failed snapshot without that marker is explicitly incomplete and cannot restore
or modify destination volumes. Keep incomplete output for diagnosis; do not
construct a manifest manually or treat existing archive files as success.

The recovery set also preserves node identity/vault, runtime secrets, media
premaster, Redis and complete ACME private state/published generations. Media,
recordings, temporary Web data and Unix certificate-runtime sockets are excluded.
Media backup/restoration is a separate operator decision. Ordinary Follower
business Backup excludes the Master premaster and is not this full recovery set.

Copy the private snapshot to another machine and verify it there. Retain old
source, Compose and images. Never remove persistent volumes as cache cleanup.

## Restore or migrate

Restore into an empty, stopped compatible Compose project:

```bash
python3 scripts/operator_backup.py restore \
  --project-directory /absolute/restored-deployment \
  --compose-file /absolute/restored-deployment/docker-compose.yaml \
  --snapshot /absolute/private-backups/before-migration
```

The tool refuses active, nonempty, overlapping, unexpected or linked targets;
it never deletes an existing deployment. Engine, SQLite path, persistent topology
and cold MySQL/Redis image identity must match. Restore media separately before
opening traffic. Preserve node/vault/premaster identity and paired credentials.
Changing a hostname is an authenticated endpoint migration, not an identity reset.
Restore ACME private and published state together; initialization recreates its
local socket. See [automatic TLS](automatic-tls-python.md) for old external pairs.

First start the backed-up release and verify recovery. For migration, keep the
hard fence and recovery set, select reviewed new images, and use normal Compose
startup. Marked schema migrations run in order and fail closed for unknown/future
schemas or missing historical secrets. A code rollback never reverses SQL;
restore the compatible full database/control set with the old images if needed.

Check schema readiness, trusted HTTPS/served-certificate match, process health
and persisted identity/ownership/accounting while the hard fence remains active.
Remove only the fence you created, then immediately verify authenticated node
health and representative Local/Direct/Relay HEAD/Range requests. Re-enter the
fence if traffic acceptance fails. Admin cannot override the hard fence; it blocks
application traffic and background business writes in both Business and Storage,
while the separate ACME controller and narrowly allowed identity probes continue.
Failed acceptance retains maintenance and recovery.
`docker compose down -v` is not an upgrade or troubleshooting step.

Uploads persist a private publication journal before linking the new file. The
managed object, public encryption descriptor and success audit commit together.
If a commit response is lost, the service retains bytes and the journal rather
than guessing whether it can delete the file. Startup recovery compares the
actual file checksum and committed object/descriptor/audit proof under the
exclusive runtime and media locks. A complete commit keeps its file; a proven
uncommitted fresh inode can be rolled back. Partial or unavailable evidence
retains the fence and reports the uncertainty. Keep the journal with the full
database/control recovery set; do not manually delete it or the media file to
clear an error. Endpoint migration refuses outstanding publication journals.

## Change a storage domain without replacing its identity

A domain identifies a network address, not a new storage owner. Keep the existing
NodeID, private key, vault, relationship, credentials, media and database. Do not
reset or pair a replacement node. Back up both the Master and Storage using the
cold procedure above before changing addresses.

After the new DNS record resolves to the storage host, change its `SERVER_NAME`
and `STORAGE_ENDPOINT`. Direct endpoints use public HTTPS 443; Relay endpoints
retain their restricted high port. Leave TLS enabled. Record the existing NodeID,
old endpoint and Master relationship ID from the authenticated node status.

Run the storage host's offline operation with the actual project and overlays:

```bash
python3 scripts/operator_storage_endpoint.py storage-endpoint \
  --project-directory /absolute/storage-deployment \
  --compose-file /absolute/storage-deployment/docker-compose.storage.yaml \
  --confirm-node-id EXISTING_STORAGE_NODE_ID \
  --previous-endpoint https://old-storage.4399.com:8443 \
  --endpoint https://new-storage.4399.com:8443
```

IDs above are placeholders for the exact existing 32-character identifiers. The
tool stops writers, acquires the persistent runtime lock and changes only the
confirmed Follower endpoint and audit. It refuses outstanding uploads, deletes,
recordings, reservations or unrecognized state. It never initializes a database,
migrates a schema, generates keys or starts services after failure.

Start the storage project with its normal Compose command while keeping the hard
maintenance fence. The certificate controller automatically validates the new
domain and issues its certificate. Verify a normally trusted TLS handshake and
the authenticated existing Follower identity at the new endpoint before changing
the Master. A diagnostic precheck alone is not issuance or readiness proof.

Then run on the Master host:

```bash
python3 scripts/operator_storage_endpoint.py storage-rebind \
  --project-directory /absolute/master-deployment \
  --compose-file /absolute/master-deployment/docker-compose.yaml \
  --confirm-node-id EXISTING_MASTER_NODE_ID \
  --relationship EXISTING_DOWNSTREAM_RELATIONSHIP_ID \
  --previous-endpoint https://old-storage.4399.com:8443 \
  --endpoint https://new-storage.4399.com:8443
```

The Master verifies normal TLS and an Ed25519 challenge against its existing
peer ID/key, then rechecks identity, credentials, role, mode and endpoint inside
the transaction. Only the relationship endpoint, reconnect health and audit
change. Repeating a successful operation creates no additional identity or audit.

Restart the Master normally and verify process/TLS readiness, certificate status
and persisted object ownership/counts. Remove the specific hard maintenance
fences, then verify online relationships and representative HEAD/Range bytes;
restore the fences on failure. Keep both recovery sets until acceptance succeeds. A
failure leaves maintenance active and does not silently reset or downgrade TLS.

## Frozen images and future use

Verify release checksums before `git bundle verify` and `docker load`. A source
installation uses `docker compose up -d --build`. Offline recovery uses the
recorded clean image override with `--no-build --pull never`; include matching
Python Web, Nginx, Redis and STUN, plus MySQL for an original MySQL deployment.
Keep independent copies of image exports rather than relying on registry tags.
Equal Python/Gin schema generations do not authorize interchanging databases,
vaults or images; an audited converter is required for that separate operation.

The owner can unarchive in GitHub Settings, restore the verified Git bundle if
needed, and fetch retained `main`/`dev`. Historical Gin refs stay preserved in
the bundle without becoming active implementation branches. Before new changes,
review dependency/security advisories and rerun complete exact-source unit/native
acceptance, then use the same reviewed `dev -> main` promotion.

Temporary playback diagnostics stop accepting reports after 2026-10-15. Fixed
clock-boundary tests cover that retirement; it does not prevent future installs.

## MasterLocal upload publication recovery

MasterLocal uploads persist a version 2 private publication journal before
linking bytes without overwriting an existing file. The journal binds the
existing upload, media and member IDs to the path, byte count, SHA-256, public
encryption descriptor and staged device/inode. It contains no encryption keys.
The file and every publication directory up to the media root are synchronized
before the local object, global catalog, quota, completed upload session and
publication-specific success audit commit in one database transaction.
After that commit, the staged link is removed and its directory synchronized
before the journal is cleared, so a crash cannot orphan an untracked upload link.

Cancellation waits for that publication worker to finish or retain an explicit
unknown outcome. A lost commit acknowledgement keeps the bytes and journal;
expired-upload reclamation refuses outstanding publication journals. On normal
startup, the exclusive runtime/media fences and per-upload kernel claim protect
reconciliation. Matching completed session, local/global object, descriptor,
audit, quota and actual byte hash prove a commit: preserve the file and remove
only its journal and any matching staged link. A proven uncommitted reservation
with no registered object or success audit permits deleting only its original
fresh inode and transactionally releasing its quota and descriptor. Recovery is
idempotent and never charges a completed upload again.

Unavailable database evidence, partial registration, missing success audit,
changed bytes or an unrelated replacement inode retain the journal and recovery
fence. Restore consistent database/control/media evidence and restart for
verification; never delete a journal or media file manually to force progress.
