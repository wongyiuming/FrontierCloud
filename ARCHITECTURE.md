# FrontierCloud Architecture Boundaries

This document records invariants that must survive feature work. It is intentionally stricter than a feature list: contributors should understand **what FrontierCloud is allowed to become** before changing cross-cutting code.

## 1. Roles and authority

FrontierCloud runs as one of three roles:

- **Standalone** — one node owns business state and local media.
- **Master** — the only business authority in a cluster. Public pages and business APIs are served by the Master.
- **Follower** — a resource node paired to a Master. A Follower provides authenticated health, storage/data-plane, backup, and node-control capabilities; it is not a second business authority.

A cluster has exactly one business Master. Do not introduce multi-Master, implicit leader election, automatic Master failover, or peer-to-peer business writes under an unrelated feature change.

Fixed Master/Follower roles require certificate-verified HTTPS. Loss of the TLS requirement must fail closed rather than silently downgrading a fixed role.

## 2. Business state versus resource state

The Master owns business truth, including lyrics and lyric relations, playback/business facts, karaoke users/recording business state, Admin audit facts, and the global media catalog.

Followers own only the resource state required to serve their assigned data and maintain the authenticated relationship with the Master. A Follower must not become an alternate source of truth for Master business records.

## 3. Storage model

The Master exposes one logical storage pool containing Master Local plus enabled Followers.

- One logical media path identifies one complete media object.
- One complete object belongs to exactly one storage member at a time.
- Placement is transparent to playback/download: Local, Direct, and Relay are transport choices, not different business objects.
- Storage allocation is FrontierCloud logical capacity. Physical disk capacity is observed separately.
- Admin Storage wording is intentionally split into `physical used / all` and `used / allocated`; do not conflate filesystem free space with FrontierCloud quota.

### Upload site types

Admin media upload chooses a **site type**, never a concrete storage member. The selector starts empty and media upload does not begin until a type is explicitly selected.

The only supported site types are:

- `primary` — the Master Local placement (`member_kind=MasterLocal`, transport `Local`);
- `direct` — Follower placements whose current transport is `Direct`;
- `relay` — Follower placements whose current transport is `Relay`.
- `s3` — enabled, connection-checked private S3 placements; browser bytes go
  directly to the object endpoint, with no Follower relationship.

For Direct and Relay uploads, the user must not name a specific member. FrontierCloud selects among all members of the requested type that are storage-enabled, online, writable, and have enough writable capacity for the object. Placement prefers the lowest current `(used + reserved) / allocated` pressure and then more available bytes, for an empty immediate-parent media folder. Existing active objects and every unreconciled reservation pin that folder to its current owner. An offline, full or conflicting owner rejects the request; FrontierCloud never scatters siblings to work around that condition. Member selection plus durable upload reservation is serialized by the storage write lock so concurrent Admin sessions see current `reserved_bytes`.

Historical media requires **no migration** for this feature. Existing `storage_member_id`, `member_kind`, and `transport` remain the source of truth; Admin derives the visible site type from those fields. A missing transport in a local/Standalone media tree is treated as primary/local. Changing this presentation must not rewrite historical ownership.

Site-type colors in Admin are observation aids only: local/primary uses a muted green marker, Direct a muted amber marker, and Relay a muted red marker. They must remain low-saturation badges rather than full-row alert colors.

Compute Worker is **retired**. Worker slots, leased compute scheduling, worker UI, or product-level Compute configuration must not be reintroduced accidentally through compatibility code.

Historical compute tables and rows remain part of upgrade and recovery data.
Runtime registration, heartbeats and configuration do not read or rewrite them,
and no job lease/completion endpoint or executable helper remains. An older
Python peer may receive the fixed disabled compatibility envelope; requests to
enable compute are refused before any resource configuration changes.

### Optional S3 adapter candidate

S3 is a separate private-bucket adapter, not a Follower with invented heartbeat,
physical free space, live traffic or certificate-controller status. Master owns
configuration, catalog authorization and durable upload state. Browser media
bytes go directly to the configured verified-HTTPS endpoint; browser encryption
and original ciphertext remain unchanged. The product does not install an S3
server or import unrelated bucket objects. Credentials use the node vault and
must be excluded from status, audit details and error messages.

Overlapping managed prefixes in a bucket cannot have separate member owners.
Unreconciled uploads block credential replacement, endpoint changes and removal;
owned objects block endpoint/bucket/prefix changes. Disabling new placement does
not revoke existing object ownership. Identity reset cannot orphan configured S3
members. A narrow recovery-credential path is allowed only after disabling the
member, putting every unfinished upload into cancellation, and expiry of all
old part grants. It cannot change namespace or authorize new uploads. This is
required to clean up with revoked or expired credentials without losing state.
The candidate supplies multipart transactions, published read/delete adapters,
Admin metadata routes, native configuration controls and a bounded browser
uploader/recovery worker. Its implementation, complete browser matrix and release
criteria are tracked in `docs/python-s3-storage.md`.

## 4. Heartbeat and Backup

Heartbeat is node-health control traffic. Backup is asynchronous recovery work. They are independent failure domains:

- a successful heartbeat stays successful even if Backup subsequently fails;
- Backup work must not block the heartbeat loop;
- full Backup construction is serialized to avoid multiple large in-memory business backups being built concurrently;
- interrupted receiving generations are explicitly aborted/cleaned when supported.

Backup artifacts are bounded business-recovery packages. They are **not** online replicas, HA storage, or automatic failover.

## 5. Managed media mutation

Managed filesystem paths and database metadata form one business object lifecycle. Direct filesystem changes are not a supported substitute for FrontierCloud mutations.

Deletion uses the recovery journal/quarantine transaction path. Rename must update both bytes and metadata or roll back.

### Folder rename

Folder rename is deliberately narrower than a general move API:

- only supported managed `music`/`vido` folders are rename targets;
- the parent directory does not change;
- target collisions are rejected;
- active uploads and pending deletion state block rename;
- a Master coordinates all storage members containing that logical folder;
- if a later member or Master metadata commit fails, already moved members are rolled back in reverse order;
- offline Followers block a rename that cannot be completed safely.

All Master path mutations participate in one process-local reader/writer fence. Cross-member folder rename owns the exclusive fence from preflight through commit/rollback. Upload-session reservation, global delete, hide/unhide, and priority mutations enter through a shared fence; upload reservations and `pending_delete` then remain durable database fences after that short shared section ends. A new path-mutation entry point must join this protocol instead of creating an independent race window.

The candidate resource-migration layer adds durable whole-parent scopes and source
drains. New uploads must check both before reserving capacity. File mutations
check their immediate parent; directory mutations also check descendant units.
Node mode, allocation, revocation and identity reset must refuse unreconciled
migration material. Identical heartbeat configuration remains idempotent and
must not clear reservations or drains. Scope/drain age is never permission to
discard them. Master-owned visibility, priorities, lyrics and playback facts
remain available and must survive ownership cutover without being rewritten
from a historical plan. Reads are not blocked by write exclusions.

Admission must use the same exclusive media lock and node-identity transaction
as these checks before execution can be exposed. The current candidate supplies
readonly planning, ordinary-write exclusions and durable Master/native preparation.
The Master coordinator preserves one task across lost acknowledgements and
process restart, repeats observations and waits for both native confirmations.
While a scope list is incomplete, ordinary path mutations fail closed. Source
drains, parent scopes and target quota survive restart and can be released only
by controlled reconciliation. Prepared tasks cannot be claimed by a transfer
runner. The execution coordinator repeats observations, verifies private staging
writability and a signed direct Source/Target HTTPS path before queueing. Master
issues short task/epoch/object/range-bound grants; Target proves its own private
key and pulls Source bytes without receiving a Master relationship credential.
Copy uses bounded buffers and incremental content verification. Verified copies
remain private staging material. Execution first registers the entire admitted
object metadata roster at both peers without copying resource bytes. It then
copies, verifies, publishes and commits one complete immediate-parent unit before
copying the next. A later parent's incomplete copy cannot block an earlier
parent's verified cutover. Task progress never implies that all future parents
have verified copies. Per-unit publication and commit checks remain mandatory;
global publication flags do not authorize modifying an already committed unit.
Explicit cancellation first fences both peers,
joins active I/O, and deletes only identity-verified private staging. Unknown
files or changed roots retain reservations. Source originals are never deleted
by cancellation. The Target can assemble verified files by hard-link/unlink,
atomically publish one immediate-parent directory and register native resource
identities. It keeps the same bytes, inode and encryption descriptor, and never
imports Master business rows. Filesystem intent and SQL phases are reconciled
after restart or a lost acknowledgement. No payload-sized second copy is made.
This relies on durable managed-write exclusions and does not coordinate arbitrary
external filesystem writers. Target publication retains all reservations; fresh
Source verification is bounded and binds current native identity, metadata and
directory membership to the original admitted unit digest. It does not rehash
unchanged payloads. Master then changes complete-parent placement, committed
bytes and its exact quota in one transaction, preserving current business and
encryption rows. Native acknowledgements consume Target capacity once; lost
responses and a new runner must reconcile the same immutable commit record.
Source originals, drains and every parent scope remain through the cleanup
grace. Light acknowledgement does not wait for the heavy-work traffic window.
The catalog cache namespace includes durable committed-byte totals, so a runner
exit or unavailable Redis notification cannot expose the previous placement.
Ordinary authenticated media and recording reads now retain durable capability
leases. Native ASGI delivery tracks the complete response lifetime; the local
Nginx counter additionally tracks active business requests across graceful reload
and worker death. Missing or stale observations never mean zero readers. A Source
cleanup intent must serialize with new leases before any removal is possible.
The candidate supplies per-unit managed Source cleanup with a durable unlink
intent, fresh Target proof, exact Source identity and active-reader checks.
Filesystem completion does not retain a node-row database lock. A restart
reconciles a known missing file against its own intent and a fresh complete
roster; unknown or changed material is preserved. Native usage is not clamped
from a stale cache while replicas are removed. Task completion separately binds
the original Source roots, performs a bounded empty physical/registry census,
reconciles native Source usage, repeats Target membership/version checks, and
requires both durable signed terminal receipts. Only this task's own capacity,
scopes and migration drain may be released. Terminal replay never mutates later
usage or new material. Actual one-hour native acceptance remains a release gate.
A historical publication receipt or a cleaned unit alone cannot authorize task
completion or independent retirement.

Independent retirement binds one exact Follower identity and relationship to a
durable operation. Ordinary node authority is fenced after preparation. Only the
exact versioned retirement POST may authenticate a fresh HMAC nonce for that same
binding; a completed peer permits status and receipt replay, never a new operation
or ordinary resource access. Authentication and each business transaction repeat
the relationship/credential binding. Final completion owns the native request
exclusion, managed writer barriers, current SQL/history and selected-file census.
Master requires the peer's signed durable receipt and repeats its own checks.
Replay verifies current identity, roots and database selection without removing
later external material. No host, container, volume, identity or audit is deleted.
Detailed protocol, implementation state and remaining Admin/acceptance gates are
in `docs/python-node-retirement.md`.

Readonly retirement preflight cannot reserve resources or authorize final
completion. The Master Admin workflow requires exact member identity confirmation,
repeats checks and resumes one durable operation after a lost reply. Legacy
revocation can cancel pending pairing or acknowledge historical revocation only;
an active relationship must use independent retirement. Known security control
projections must match bounded current SQL state; directory/name exemptions are
not physical emptiness proof.

The Master runs one serial migration scheduler independently of heartbeat and
Backup. It drives short durable control steps; Source and Target carry the
payload directly. Network loss retains the journal, scopes and capacity.
Integrity failures require an explicit revision-checked retry. Manual pause is
durable across restart and cannot be undone by a traffic observation. Copy,
verification, publication and delayed cleanup may pause between bounded steps;
pausing never rolls back ownership or removes published data. A stale runner
cannot record progress after a control revision changes. Lightweight native
acknowledgements may settle an existing commit before heavy-work traffic pause.
Post-publication cancellation requires its own controlled reconciliation; a
generic journal state change cannot authorize deletion or scope release.
Cancellation reconciles each parent independently: committed placement stays
final, while an uncommitted private parent needs fresh Source retention proof
and identity-pinned removal intents. Its complete logical roster is distinct
from actual partial bytes removed and copies that never existed. Such a task
must not invent verified copies or claim deletion of the full logical size.
Only signed native settlements may refund that parent's logical reservation.
Terminal replay checks durable evidence without a new unlink or quota mutation.

### Process model for mutation safety

The production Web service must run a **single ASGI worker**. The media mutation fence above is process-local; starting multiple Uvicorn/Gunicorn workers or setting `WEB_CONCURRENCY` would create independent locks and silently invalidate rename/upload/delete/hide serialization.

Do not add multiple Web workers as a performance tweak. Multi-process or horizontally scaled Web execution requires replacing the process-local media mutation fence with a database-backed or distributed lock, plus new cross-process concurrency regressions, before deployment topology changes are allowed.

Do not turn folder rename into an arbitrary cross-parent move without a new transaction design and federation regression coverage.

## 6. Directory priority

Folders are first-class sorting objects. Directory priority is distinct from media-file priority and uses the same bounded preference range.

Public category/subcategory ordering is `directory priority descending -> name`. Priority state is stored as managed metadata and must migrate with a successful folder rename.

Catalog ordering is cached server-side. A cache hit must not cause a new MySQL sort query; changing priority or a directory name must invalidate the catalog generation.

## 7. Lyrics

Lyrics are Master-owned business content even when media bytes are stored on Followers.

The supported hierarchy is intentionally bounded:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum. Upload, validation, Admin tree, scoped search, download/delete collection, lyric catalog, and relation management **must all enforce the same hierarchy**. It is forbidden for one surface to accept a path that another surface cannot manage.

`lyrics/default.lrc` is an internal, automatically maintained playback fallback:

- it is not user lyric content;
- it is excluded from Admin lyric counts and relation counts;
- it is excluded from Admin media-tree/search presentation;
- it cannot be selected as a normal delete/download target;
- a fallback relation does not make a track appear to have a user-managed lyric in Admin.

Same-name auto-link is fallback automation, not an authority over user decisions. It runs only after an administrator explicitly clicks the Admin auto-link control; lyric upload and catalog refresh must never trigger it. It may fill a missing relation or replace `lyrics/default.lrc`, but it **must never overwrite an explicit user-managed lyric relation**, even when that selected lyric file is temporarily unavailable. The current-relation check and replacement happen under the same database row lock so a concurrent manual link cannot be overwritten. Auto-link must scan only the supported media and lyric hierarchy; historical orphan files outside that hierarchy cannot re-enter business state through automation.

An accepted lyric upload is successful only after the file and its managed-object registration are durable together. A failed registration/audit transaction removes the newly published file. A later cache-invalidation failure must never delete an already committed lyric.

## 8. Catalog and cache consistency

Redis catalog generations cache expensive scans/sorts. Browser/API cache headers must not make mutable directory structure remain stale after a successful Admin mutation.

For mutable catalog APIs, the HTTP client revalidates while the server-side Redis generation remains the expensive-work cache. Mutations that change paths, visibility, priority, or catalog membership invalidate the generation. Cache invalidation is fail-soft after a durable business commit: a Redis failure is logged and TTL recovery remains available; it must not turn a committed mutation into a false client-visible failure.

## 9. Playback continuity

Compatible MP3 playback is one continuous browser media session: one audio element, one `MediaSource`, and one `audio/mpeg` `SourceBuffer` in sequence mode. A track boundary updates business metadata and accounting; it must not recreate the player, switch `src`, call `load()`, or introduce a second standby element.

The browser keeps a bounded playback window rather than downloading an album into an unbounded SourceBuffer. Appends are backpressured by the playback clock, old ranges are pruned after a boundary, and `QuotaExceededError` retries the same bytes silently without marking a track as failed.

The player exposes stable track-local duration and time over the global MSE timeline. A seek inside the current buffer moves that timeline directly. A seek outside it restarts the same logical track with an open-ended HTTP Range request near the requested byte position, preserves playing/paused state, and resumes a transiently interrupted range from the explicit base plus delivered bytes. It must not clamp the request to the buffered edge or show a playlist failure notice for a recoverable read.

The initial continuous profile is MP3-only. Other accepted audio formats remain manually playable through the single-track fallback and are visibly skipped by automatic continuation. Video remains outside this architecture.

The complete browser and release contract is documented in [`docs/audio-continuous-stream.md`](docs/audio-continuous-stream.md) and [`docs/wiki/Playback-Continuity.md`](docs/wiki/Playback-Continuity.md).

## 10. Admin GUI contract

The Admin console order is an intentional product contract and is tested in both source and real Chromium layout:

1. Media management
2. Media sorting policy
3. Lyric relations
4. Karaoke users
5. IP security
6. WebRTC network relations
7. Nodes
8. Resource migration
9. Site access state
10. Admin Key
11. Brand Logo

Dynamic modules must join this same final DOM/visual order. Reordering code must be idempotent; DOM mutation observers must not create self-triggering reorder loops.

## 11. Release topology

The repository has two canonical branches:

- `dev` — implementation and bounded source CI authority;
- `main` — reviewed release history.

Absolute repository policy:

- **Do not create any new branch.** Additional feature/fix/release/temporary branches are prohibited.
- all changes go to the existing `dev`;
- the only PR into `main` is same-repository `dev -> main`;
- after the release PR is merged, fast-forward `dev` to the resulting `main` commit before further work;
- never force-rewrite `dev` or `main`.

GitHub-side ref creation cannot be fully prevented by a unit test. The repository-policy workflow detects non-canonical branch creation after the event, while true pre-creation prevention requires GitHub repository ruleset/administrative enforcement. The no-new-branch invariant must remain documented here, in `CONTRIBUTING.md`, and in the Wiki/ruleset configuration.

Hosted test jobs have a three-minute maximum and bounded 150-second commands.
Source and Compose-configuration checks run in parallel, without complete unit
discovery, image builds, live services, real databases, Chromium or recovery and
hour-long lifecycle fixtures. Complete exact-source acceptance remains required
on the development host before release. Public CI/source provenance does not
claim that this separate native gate has passed.

## 12. Regression rule

When an invariant can be encoded as a test, encode it. When it cannot be reliably observed from repository code (for example, who is allowed to create a Git ref), document it explicitly and enforce it with GitHub repository settings where available.

A regression may not be weakened merely to accommodate a new feature. Architectural changes require an explicit architecture update and corresponding test changes in the same development cycle.

## Frozen Python release contracts

This repository's final runtime remains Python/FastAPI, with one ASGI business
worker. Business HTTPS uses Nginx; Storage uses Python's native TLS server.
Published Business and Direct HTTP/HTTPS are fixed to 80/443. Relay Storage exposes
only its authenticated private node transport. Product components run through
Compose, without host systemd, Docker sockets or privileged mode.

The optional resource-migration scheduler observes real ongoing HTTP I/O, not
completed access logs. The approved Nginx C observer is built only in the Nginx
image builder; the runtime retains Python business/control/TLS processes and
sendfile. A read-only shared Unix socket exposes bounded byte counters only.
Native Storage is measured at ASGI receive/send. Missing or stale observations
stop background work; a restart requires a new continuous quiet interval.
Traffic observations must never resume a manually paused job or grant access.

TLS_ENABLED is the sole TLS switch. Automatic Python ACME owns durable accounts,
private cache, certificate validation, retries, atomic publication and reload.
User certificate paths and public port overrides are removed; the environment
validator diagnoses retired settings instead of accidentally reading old keys.
Failure never opens plaintext business traffic or disables peer TLS validation.
Certificate status is read-only; an authenticated Admin may download only a leaf
certificate/key pair through a browser-bound encryption envelope.

Each upload selection explicitly chooses `plain` or `encrypted`. Encrypted bytes
leave the browser as AES-256-GCM chunks, using the fixed versioned Gin browser
protocol; the Python backend wraps file keys and checks metadata, ownership,
ciphertext size/hash and lifecycle consistency. It does not routinely decrypt
media. Fixed 15-minute authorization is consumed for one browser session's grant
scope covering multiple current file requests. Expiry/revocation blocks new key
grants; it cannot erase browser-retained keys or plaintext.

Media file IDs and nonce prefixes cannot be reused. Deletion preserves an
immutable encryption tombstone; rename preserves identity. Premaster secrets
remain only on the business node and in separately protected operator recovery
artifacts; they must never be included in ordinary Follower business backups.

Priorities use bounded integers from -7 through 500000 and inherit every stored
historical value. Ties have canonical path/identity order independent of browser
sessions. Drag requests carry the complete sibling-scope revision; hidden and
off-page siblings remain part of that revision. The minimal collision window and
its bounded audit records commit atomically, or the entire request rolls back.

A timeout only selects upload recovery candidates. Kernel writer leases and an
authenticated physical reconciliation result decide whether bytes and quota can
be released. Refresh, process death and expired credentials cannot strand a
unique path forever, and live writers cannot be reclaimed based on age alone.

Software replacement is an operator action. The online updater, upgrade/rollback
APIs and Admin module are retired. Explicit backup/migration remains available;
an operator maintenance fence always wins over manual Admin availability.

The approved Python operator is a one-shot host entry invoking local Compose.
It is not a daemon or systemd service, and business containers receive no Docker
control permission. Host journals and source/recovery material remain private
and outside mounted business data. A durable operation reference is loaded
before live discovery when resuming interrupted work; image tags or incomplete
container labels cannot substitute for its pinned source/configuration proof.

Upgrade recovery captures control state, SQL, identities and keys, excluding
media and recordings. Controlled pre-opening restoration retains failed roots
and occupied volumes, restores old state only into empty replacement resources,
and journals same-filesystem no-replace payload-directory moves. It never
deletes the failed deployment or infers a DDL downgrade. Nested mounts, foreign
leases, changed ownership or competing destinations refuse. Selecting recovery
blocks forward upgrade. The persisted opening boundary forbids old-state
restoration even after a lost reply, because new business writes may exist.
Implementation status and native gates belong in `docs/python-source-operator.md`.
