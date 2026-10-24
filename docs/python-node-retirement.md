# Independent node retirement candidate

This is a Python contract and implementation ledger. Gin remains a separate
project. The candidate exposes operation-bound internal retirement, readonly
preflight and Admin check/confirm/retry/history controls. Their complete native
and release acceptance remains required. The terminal core
combines request-lifetime exclusion, fresh SQL/history reconciliation,
physical inspection and durable signed receipts as described below. The separate
checks cannot be used as standalone retirement approval.

Retirement targets one exact downstream Follower identity. S3 configurations
have a separate disable/settle/remove lifecycle; an object bucket is not a native
node with a physical filesystem or relationship heartbeat.

## Current durable preparation

Generation 8 adds `node_retirements`. Each side retains the operation ID,
relationship, Master/member identity, public-key fingerprints, endpoints,
transport and a private credential digest. No raw credential is in the record.
Existing generation-4/5/6/7 migration definitions remain immutable.

Preparation holds the exclusive managed-media barrier and the node identity
writer transaction. It repeats logical ownership, uploads, recordings, quota,
backup and migration blockers before setting the relationship to `retiring`
and disabling new placement. Journal, relationship, storage flags and audit
commit together. A failed audit rolls the transaction back. Once committed,
retry reads the same operation and start time; neither an old observation nor
elapsed time releases the fence. Peer preparation checks the complete Master
binding against its own current relationship, credential and local identity.

Ordinary authentication, upload/recording admission, storage configuration,
pairing, activation, legacy revocation and identity reset consult durable
retirement exclusions. A stale relationship row marked active cannot bypass
the journal. Retiring one member does not fence unrelated Master members or
Master Local. Unknown files and retained backup generations/chunks are never
deleted to obtain a passing report.

The Linux request gate pins the real data-root and empty mode-0600 lock inode.
Successful Web startup registers it under the actual held native runtime lease.
Internal HTTP requests retain shared leases through complete ASGI response
delivery and joined file I/O, including cancellation. Upload/recording file
claims join the same inode for queued work and independent processes. Final
inspection requires an issued exclusive capability; a caller flag or a foreign
lease cannot substitute. The exact retirement POST is reserved to acquire its
own capability; health endpoints stay available. Operator fencing stays outermost.
Replacing or unlinking the lock, changing its mode/links/contents, replacing
the data root or reusing an inherited capability in a fork is refused.

## Startup-bound physical inspection

After native admission, Follower startup binds the actual three runtime roots,
selected database, matching node vault, any existing bound premaster and exact
built-in fallback lyric. Failed binding leaves retirement unavailable with a
specific blocker; it does not repair files or replace keys. Runtime shutdown
closes the pins and request gate after native work is stopped, including failure
paths. Certificate-controller volumes and other external secret files retain
their independent lifecycle; inspection never authorizes removing those volumes.

The physical census uses bounded nofollow pages. It reads only small known
control bytes. Media contents are not opened, decrypted, copied or rehashed.
Retained files must match pinned inodes and bytes; a valid-looking filename is
insufficient. SQLite main/WAL/SHM must additionally belong to the selected live
engine and be held independently of the observer. MySQL inspection verifies
the selected database and identities without inventing local database files.
Known empty coordination files require the expected owner/mode/link count and
an idle kernel lock. Unknown files, nonempty coordination records, links,
changed roots or busy owners block inspection and are preserved.

The exact native IP-security snapshot is a retained control projection. Its
fixed path and parent are bound at startup. Each census pins a mode-0600,
single-link regular file and compares its bytes with the canonical current SQL
ban/whitelist projection. Metadata checks are limited to 1,024 rows per source
and 64 KiB of file content; exceeding the limit refuses retirement. Replacement
during inspection, altered bytes, links, unsafe permissions and any neighboring
unknown file still refuse. A legitimate atomic publication between observations
is allowed without restarting the runtime. The observer never removes the file.

Nested inventory roots are not added twice to retained byte/file counts.
Protected external vault bytes are reported separately. Directory traversal and
temporary sidecar descriptors close on success, rejection or cancellation.
The existing managed-writer barriers and exclusive request capability remain
necessary throughout the terminal transaction. Unknown external filesystem
writers are outside the managed-write contract; the scanner is not a filesystem
snapshot or a disk-wiping authorization.

## Completion requirements

The terminal coordinator must combine the implemented request join and physical
census with media/recording barriers and fresh SQL/history checks. Only known matching
control state, selected database/WAL/SHM, identity keys and idle coordination
material may be retained. Filename-only exceptions cannot establish safety.
Unknown files, links, changed roots, unjoined readers/writers, pending uploads,
backups or migration work block completion and retain the fence.

Completed migration history uses the bounded validators described below;
terminal state labels alone are insufficient. A prior migration cleanup or an
old retirement preflight cannot authorize independent retirement. Nginx-served
native storage cannot complete until its active readers participate in the same
final barrier.

The peer must persist a terminal receipt under its exclusive request capability.
Master completion requires that authenticated receipt with matching operation,
identity and roots. Revoked credentials may only replay/query this exact receipt
with a fresh request nonce; ordinary requests stay denied. Completion timestamps
and audit records must be idempotent across timeout and restart. No host,
container, volume, certificate controller, database or audit history is removed.

## Internal retirement protocol

Only `POST /internal/v1/retirement` accepts these controls. The exact JSON envelope
contains format/version, `check`, `begin`, `status` or `complete`, a fresh request ID and
the complete retirement binding. Identities, key fingerprints, canonical HTTPS
endpoints, mode and integer protocol/version fields must match. Duplicate JSON
or authentication headers, nonfinite numbers and boolean/float versions are
refused. Bodies are limited to 16 KiB and five seconds of reception. TLS is
mandatory; no plaintext or certificate-verification fallback is added.

`node_retirement_auth.py` uses the existing relationship HMAC over method, exact
path, raw body, timestamp and nonce. It shares the durable nonce table with
ordinary controls and retains accepted nonces through the inclusive future-skew
boundary. Reaping is limited to 32 expired rows of that relationship. The current
Follower/upstream identity, credential digest and stored operation are checked
under the identity-writer transaction. Business preparation, status and completion
then repeat the authenticated relation/credential snapshot; a credential replaced
between authentication and mutation cannot authorize that mutation. Status cannot
return a different operation created in that interval.

The retirement route owns its request lease through actual response delivery and
cancellation. Begin/status retain shared ownership. Check/complete authenticate first,
then acquires exclusive ownership; an active ordinary request returns a signed
busy blocker. Missing startup Authority cannot create a Gate-only journal. After
completion, only status or complete may replay that exact receipt with a new nonce.
Ordinary authentication, pairing and resource access remain fenced. The actual
database selection is reverified even during receipt replay; copied identity and
journal rows cannot substitute for the selected live database.

Replies contain signed operation/request bindings, phase, timestamps, root digest
and an optional durable receipt. Private roots, credentials and keys are not
returned. Authenticated errors retain bounded signed application codes tied to
the request bytes. The client verifies the signature, identities, request ID and
body digest before accepting a result, and independently verifies completed peer
receipts. Replaying a valid older reply for a new request is refused.

`node_retirement_client.Coordinator` queries peer status first. A verified
`retirement_not_started` leads to preparation of the same operation, followed by
completion and the fresh Master final transaction. A lost response is recovered
by status and the durable receipt. A completed Master replays locally without
contacting the peer. This is server control logic, not a new host daemon or foc.

## Admin operations

On the Master, open Nodes, expand the selected Follower and choose Safe retirement.
Check reports Master and peer observations separately. It changes no business
journal, relationship, desired configuration, capacity, ownership or file. HMAC
authentication consumes its normal nonce; the ephemeral binding is not a stored
plan or later permission. A blocked stage stops there; later checks are not
implicitly reported as passing. Unknown and unreachable states are not ready.

Type the full displayed member NodeID and confirm. The server repeats preflight,
durable preparation and both final checks. An intervening upload, resource or
file change may still refuse. After preparation, a lost reply or blocker retains
the same operation and fences; Continue retirement queries peer status before
advancing. There is no force/drop-data or timeout release button.

Retirement history is paged at 20 operations for the current Master identity.
It includes incomplete operations and completed receipt downloads. The backend
revalidates each stored binding and signature rather than displaying a raw
database success flag. Historical identities' rows and audit remain stored;
the current signing key cannot newly verify another identity's old history.
Ordinary active-node revocation is removed from both Admin controls and legacy
state/peer APIs. Canceling a pending pairing and acknowledging an already revoked
historical relationship remain supported without changing media or identities.

These controls use the existing Admin session, CSRF, HTTPS and activity policy.
They never ask the browser to manage operation IDs, files or relationship secrets.

Admin V7 passes 143 focused SQLite/Linux cases without skips in 232.371 seconds,
plus 47 route/deployment regressions. The selected-file cases include a SQL-bound
IP-security snapshot, legitimate republishing, altered bytes, unknown neighbors,
unsafe modes/links, replacement during a census and bounded metadata refusal.
The actual trusted Chromium/Compose Master and Direct pass 21 checks on Browser
V8, including the real Admin legacy route refusing active-node revocation,
preflight blockers, exact confirmation, real CSRF enforcement, receipt download,
history and retained identity. No API response is fabricated. An initial test
client used a separate HTTP trust store; that CSRF check now runs inside the real
page with the browser's normal CA validation. TLS errors are never ignored.

Both Web containers are limited to one CPU and 512 MiB. Five consecutive final
samples are below 20% of one core; earlier request/heartbeat samples above 30%
are retained in the raw evidence rather than omitted. This milestone does not
establish the full Direct/Relay fleet matrix, actual Nginx terminal-reader
acceptance or complete release readiness. The same V7 backend passes 17 actual
MySQL file cases and 40 two-store API cases, plus active-request and lost-reply/
restart cases through actual verified TLS on each database engine. These retain
34 HTTP inputs and eight actual Browser V8 Admin requests. Full release evidence
remains a separate gate. Production, Gin and the owner preview remain unchanged.

API V6 passes 150 focused SQLite/Linux cases without skips, including 21 shared
API/nonce cases and one additional SQLite database-substitution case. The same
21 API cases pass on actual separate MySQL stores. Actual certificate-verified
HTTPS/HMAC runs both active-request and lost-response/restart scenarios on each
database engine. Every scenario completes both peer and Master; the restarted
peer refuses a persisted old nonce and returns the identical signed receipt.
Eight wrong-CA/hostname handshakes fail actual certificate verification.
The four scenarios retain 34 actual HTTP inputs, including binding JSON and
short-lived test authentication headers. Each then records five one-second CPU
samples below 20% of one core. Full native-gate scripts include these SQLite and
MySQL TLS cases in one-CPU, 512-MiB client/Web fixtures with a separate constrained
MySQL server. Source policies, shell syntax/diff and migration signatures are
verified separately with the source inventory. Exact inputs and scoped evidence
are private in `retirement-api-progress-evidence.json`.

These results do not establish readonly preflight, Admin/browser operation,
legacy active-revocation replacement, actual Nginx terminal-reader acceptance or
complete release acceptance. Those remain required. Production, Gin and the
owner's S3 preview are unchanged.

## Earlier terminal core milestone

`node_retirement_complete.py` requires the actual native runtime, its startup-bound
Authority and an issued exclusive request capability. It retains the media and
recording barriers and identity-writer transaction throughout repeated logical
checks, bounded immutable-history validation and physical inspection. A Gate-only
preparation, caller boolean, shared lease or foreign capability cannot complete it.
Follower local membership is checked against the real configuration contract:
`member_kind=Follower`, `transport=Local`, no local relationship foreign key.
Master instead requires the exact remote relationship and its transport.

Active or unavailable reader observations refuse completion. A deployment using
Nginx must also supply a new gateway sample after exclusive request admission;
an older zero-reader observation is insufficient. Core tests use synthetic Monitor
samples for this boundary, not an actual Nginx terminal-retirement claim.

The peer persists a signed identity/operation/root-bound receipt and revokes its
ordinary relationship in one transaction. Master verifies that exact peer receipt,
repeats its selected-member ledger/history checks and persists its own receipt.
Storage remains disabled. Existing summaries, identities, keys, databases, audit
and terminal migration history remain intact. The revocation acknowledgement stops
legacy revocation notification retries without reopening any ordinary capability.

Core replay validates the stored signature and identity binding, returns the same
receipt, timestamp and audit record, and does not rescan or delete later external
material. Audit/SQL failure rolls terminal writes back while preserving the already
committed retirement fence. Cancellation joins an in-progress census page before
closing descriptors and rolling back. Revoked-credential access to this replay is
not exposed on that earlier snapshot; the later protocol above supplies it.

Completion V2 passes 120 focused SQLite/Linux cases in 146.922 seconds without
skips, including 21 terminal-core cases and existing fence, filesystem, request,
federation and capability regressions. Actual separate MySQL databases pass all
21 core cases plus 15 preparation cases. V3 adds two actual migration-to-retirement
cases on each engine: 32.782 seconds on SQLite and 27.513 seconds on MySQL. These
run the real migration, ownership cutover, native acknowledgements and cleanup,
then retire Source without changing retained migration history or Target bytes.
An empty Source with a forged final migration receipt is refused. Cleanup time
is explicitly virtual in these cases.

The current source also passes two actual trusted-HTTPS/HMAC request-fence races,
four wrong-CA/hostname negatives and five idle CPU samples per race, ranging from
0.105% to 0.163% of one core. This revalidates ordinary request/auth fencing; the
terminal core is called directly in its tests. Four source policies and shell
syntax/diff checks pass. Native MySQL gate wiring includes both new modules.
Inputs, exact source inventories and cleanup records are private in
`retirement-complete-progress-evidence.json`. These earlier tests did not expose
HTTP retirement or establish Admin/browser and complete release acceptance.
Production and the owner preview remained unchanged.

## Earlier runtime validation

Runtime V4 passes 128 focused cases in 90.885 seconds, with one explicitly
CI-only MySQL smoke skipped; four policies and shell syntax/diff checks pass.
The same source passes 16 actual MySQL schema cases, 15 two-database preparation
cases and 10 MySQL physical-inspection cases. These include an admitted recording
returning HTTP 409 without consuming the body or leaving a payload/reservation.

Two actual private-CA HTTPS/HMAC races retain the live handler's OS lease after
authentication and SQL fencing. Late guarded admission refuses staging; a
deliberately unguarded test-only handler demonstrates that final physical
inspection refuses and preserves its unknown file. Ordinary old credentials
remain denied. Health responds under final exclusion, and new internal work is
refused. Wrong-CA and wrong-hostname requests fail ordinary TLS verification.
After each case, five consecutive one-second CPU samples are below 20% of one
core; observed V4 samples are 0.112-0.160%. Each client/Web test container has
one CPU and 512 MiB; the isolated MySQL container has one CPU and 768 MiB.

All 592 Runtime V4 source files have an exact recorded inventory. The full
native-gate scripts now include the HTTPS race and MySQL physical module; the
complete release suite is still pending. Exact inputs, scoped results and
cleanup evidence are private in `retirement-runtime-progress-evidence.json`.
The skipped CI case is not a pass. These V4 results predate terminal coordination;
the later core milestone above does not replace HTTP and Admin/browser acceptance.
Production and the owner preview remain untouched.

## Migration history checks

`node_retirement_history.py` checks native peer reservations separately from
Master tasks. Native completion requires matching immutable per-object bindings,
unit transcripts, commit records, source cleanup intents or target publication
metadata, and signed final receipts. Released or aborted tasks may retain no
capacity, scopes, drains or live task readers. A canceled verified copy may retain
its original verification metadata as audit history; it is never treated as
remaining staging or as deletion authority. Unstarted cancellation may retain
partial accepted counters after its units have been safely removed.

`node_retirement_master_history.py` rebuilds the approved catalog and content
observation digests from the original snapshots, then reconstructs every unit's
version and transcript. It checks retained object journals, native acknowledgements,
commit records, completion census and both signed final receipts. Unstarted
cancellation has no retained unit rows, so its full transcript is reconstructed
from the immutable preview. Active tasks block only an involved member; unrelated
tasks and readonly preview caches do not become retirement blockers by age alone.
Orphan child rows are refused rather than silently ignored.

Metadata is read in pages of 16 with explicit JSON limits. Historical observation
validation uses its recorded time only to check structure and the original digest;
it does not refresh an expired capability or contact a peer. No media payload,
premaster or file key is opened, no new grant is signed, and no row is modified.
Legitimate Direct/Relay changes after completion preserve the original binding.
These validators remain independent from the terminal coordinator; passing history
alone cannot authorize retirement. Final completion still requires the live request
barrier, fresh logical checks, startup-bound physical census and durable receipt.

History V5 passes 30 focused SQLite/Linux cases in 311.743 seconds without skips:
15 history cases and 15 existing transfer-protocol regressions. The same source
passes all 15 history cases with actual separate MySQL node databases in 200.132
seconds. Cases include plain/encrypted media, opaque recording footers, full and
partial cancellation, paged plans, changed modes, forged signatures and hashes,
orphan rows, retained reservations and oversized metadata. SQLite's unenforced
VARCHAR widths are bounded in the SQL projection before values reach Python;
relationship observation summaries are not fetched.

Assertions forbid media reads, premaster/file-key use and new signatures during
inspection, and compare all migration tables before and after. Four source
policies and shell syntax/diff checks pass, and every existing migration signature
remains unchanged. Full native-gate scripts include the MySQL history module.
Inputs and scoped source/result/cleanup evidence are private in
`retirement-history-progress-evidence.json`. These are in-process history tests
with a virtual cleanup clock, not a new HTTPS or real-hour cleanup claim. Terminal
retirement, Admin execution and complete release acceptance remain pending.

## Earlier preparation validation

V5 passes 142 focused SQLite/Linux, federation, ordinary-write and recording
failure regressions in 137.146 seconds. V4 passes 16 actual MySQL schema cases
in 63.763 seconds and 14 two-database preparation cases in 94.306 seconds.
V5 retains exactly the same runtime and native MySQL inputs as V4; its changes
are the real-SQLite recording fault fixture, fixed migration-signature assertion,
native-gate script wiring and release documentation. Every test container has a
one-CPU limit. Four source policies and shell syntax/diff checks pass.

These cases cover real generation-7 to 8 preservation, SQLite DDL rollback,
MySQL committed-create retry/index repair, foreign structures, enforced checks,
subprocess/fork lock behavior, durable identity/write/auth fences and idempotent
preparation. Original rows, keys, unknown files and retained backup chunks stay
intact. Real recording fault cases verify that storage reservations return to
zero after publication failure or an existing-target conflict.

All 585 V5 source files match their recorded inventory; V4/V5 differences and
the actual MySQL fixture's removed container/network/volume identities are
recorded privately in `retirement-preparation-progress-evidence.json`. HTTP
retirement, terminal physical clearance and browser controls are not exercised.
No production or owner preview was updated; the preview remains at its earlier
generation. These are intermediate results, not full retirement acceptance or
a release gate completion.
