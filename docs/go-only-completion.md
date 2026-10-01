# Go-only backend completion gate

The target is a standalone Go backend, not a Python wrapper. The historical
Python source remains a contract oracle until parity is established; it must
not appear in the final runtime, initializer, migration, maintenance or updater.
The default is Gin + SQLite. DB_TYPE=mysql selects the same logical schema on
MySQL. Existing frontend, Nginx, Redis and coturn remain unchanged.

`protocol/v2/http-routes.json` records the effective routes (including integrity
overrides) from the reference application, rather than all decorated functions.
Each method/path must have a real implementation and behavior tests. A registered
route alone, an empty response, or a generic success response is not parity.

## Acceptance checklist

- [x] Exact canonical JSON, Ed25519, HMAC and capability conformance vectors
- [x] Independent Go image, initial secrets/media and schema migration commands
- [x] Shared logical schema, SQLite and MySQL initialization and migration tests
- [ ] Complete environment configuration and selected database deployment
- [ ] Public pages, catalog, streaming, playback, lyrics and brand
- [ ] Admin authentication, CSRF, keys, tree, upload, visibility and deletion
- [ ] Directory rename/priorities, lyric management and security observability
- [ ] Node identity, fixed role, pair/confirm/revoke/re-pair and heartbeat
- [ ] Master global catalog, storage allocations and Direct/Relay capabilities
- [ ] Recording accounts, quotas, upload/finalize/download/delete
- [ ] Logical business backups, promotion and restore with either database
- [ ] Go updater, release verification, maintenance, rollback and cleanup
- [ ] Only-Go deployment contains no Python process or Python-dependent script
- [ ] Isolated 1 Master / 3 Direct / 6 Relay private-CA integration validation

Do not switch the existing development topology to Go until the replacement
passes its integration gates. Do not claim completion from schema tests alone.

## Current verified slice (2026-10-01)

The Go runtime currently supports Standalone public media pages/catalog,
local streaming with Nginx acceleration (or Go Range serving), playback
accounting, lyrics, opaque karaoke handles, brand assets, Admin login/status/
logout, temporary keys, key rotation, and the Admin shell. Environment loading
supports `.env` with explicit process-environment precedence.

The native Admin media implementation now includes managed hierarchy browsing,
visibility, directory/media priority, phrase-aware Chinese/pinyin search,
streaming media/lyric upload, single-file and streaming ZIP downloads, and
journaled deletion. Upload/delete intents survive uncertain commits and restart;
an unresolved intent blocks media traffic/readiness instead of claiming success.
The embedded pronunciation data is immutable; Python is not invoked for search.
Directory rename preserves stable media identities, play statistics, visibility
and lyric links through a durable replayable intent. Explicit and automatic lyric
relations are transactional, including reverse edits and concurrent matching.
Cross-process volume leases prevent worker races; killed processes release their
locks and new workers preserve live multipart uploads.

IP bans, whitelist, attack accounting and policy generations are authoritative
database state. A Go publisher produces the existing Nginx TSV snapshot; Redis
outage cannot bypass a ban. Native network observations preserve verified client
identity, have Redis cooldowns and transactional grouped summaries.

Real disposable MySQL and Redis checks pass, including concurrent stable-ID
registration, exact-once playback accounting, temporary-key redemption,
session TTL, rotation, CSRF and Admin HTTP contracts. The full Go race suite
passes on Linux. Windows Go tests pass as well. The latest real MySQL/Redis run
also verifies security policy concurrency, network observations and HTTP parity.

The latest disposable Linux test run also verifies concurrent/idempotent upload
publication on real MySQL, positive multipart/CSRF upload contracts on real
Redis, exact-case metadata deletion, ZIP content and recovery fault injection.
Remote source hashes were checked against the local files. The Python reference
suite passes 567 tests (4 skipped), including shared search vectors.

The next verified domain slice includes TLS-verified, pinned Ed25519 node
challenges, canonical control HMACs, bounded non-redirecting HTTPS transport,
one-use pairing packages, encrypted relationships, durable replay rejection,
confirmation/revocation/re-pair, and outbound-only heartbeat reachability.
Master promotion/adoption is atomic with storage allocation and audit, retaining
local media IDs. Follower configuration preserves reservations and backup state;
the retired remote Compute Worker remains disabled.

Real MySQL checks also pass for site-type upload reservations, same-folder
placement affinity, category layout fences, physical/logical capacity limits,
and idempotent global publication. Unknown expired physical placements retain
their reservations until confirmed cleanup.

Native HTTP pair/confirm/revoke/heartbeat handlers now use bounded raw control
bodies and exact-path HMACs. The global public catalog, virtual category/player
pages, exact-once playback, default/explicit lyrics, Direct capabilities and
Nginx Relay destinations are implemented. Follower media serves verified
capabilities with strict paired-origin CORS, Range and HEAD, never a public
independent business catalog. Master Admin virtual tree/search, visibility,
priorities and lyric management retain global IDs without manufacturing local
identities for remote tracks.

Follower uploads reserve quota before streaming, use bounded buffers, persist
publication intents and exact object IDs, and atomically commit accounting with
audit. Lost-response retries do not replace or re-charge files. Startup recovers
completed intents and abandoned reservations while preserving another worker's
live OS lease. Real MySQL verifies concurrent owned upload reservations and
idempotent publication; SQLite fault tests cover interrupted/lost commits.
The HTTP pair → heartbeat/configuration → signed upload → authenticated stat →
global finalize → Direct Range/HEAD chain passes; Relay remains Nginx delegated.
Master Admin session uploads now support Primary, Direct and Relay placement,
authenticated Direct stat/finalize, private streaming data-plane connections,
idempotent completion and confirmed-physical-cleanup cancellation. MasterLocal
durable intents atomically publish local/global identities and quota even after
expiry or lost commit responses. Concurrent cancellation cannot race a live
transfer or unlink an unknown file. Follower deletion uses a replayable quarantine
and atomic quota refund; pending crash intents restore bytes, committed intents
clean them without resurrection. Native HTTP and SQLite recovery checks pass;
the upload/session and Follower deletion slice also passed real MySQL, Redis
HTTP integration and the complete Linux race suite.

Global deletion now persists pending placements before any physical operation.
Offline/uncertain remote bytes retain their path and capacity; a bounded native
background retry converges confirmed cleanup. MasterLocal quarantine commits
global metadata, playback/lyric cleanup, local identity and capacity atomically.
Interrupted pending intents restore bytes; committed intents only clean up.
Missing physical bytes converge against the accounted object identity/size.
Exact-case scopes, SQL audit rollback, quota replay and mixed Primary/Relay HTTP
deletion tests also passed real MySQL, Redis HTTP integration and Linux race
validation in an isolated environment.
Master/Follower legacy mutation entry points reject quota-bypassing operations.

Native Karaoke account registration/login/status/logout, captcha images,
password changes, Admin user search/ban/unban/quota and durable user deletion
state now use the Go domain repository. Password hashes match Python scrypt
vectors; NFKC/casefold usernames and default quota retain the existing contract.
Daily registration limits and account audits are atomic on SQLite/MySQL. Native
Redis sessions use CSRF plus password fingerprints/revocation generations, so
password changes or ban/unban never revive old sessions. Legacy sessions require
re-login. The account slice passed real Redis/MySQL and Linux race integration.

Native recording tickets reserve personal and member capacity atomically;
Primary, Direct and Relay uploads use bounded private storage and a separate
TLS-verified data-plane pool. Server-side stat/finalize preserves exact-once
quota accounting, validated Python-compatible recording footers and private
listing/Range/HEAD/download contracts. Opaque Karaoke context/stream/lyrics are
native too. CSRF and authenticated owner checks run before body streaming.

Local recording publication/deletion journals survive interrupted requests,
restart and lost commit responses. OS leases preserve another worker's live
stage; unresolved or busy intents retain the readiness fence. Remote cleanup
retains capacity until an authenticated physical receipt, with bounded native
retry and durable account deletion completion. Follower recording/owner
tombstones reject still-valid old upload capabilities after cleanup, including
never-uploaded cancelled tickets. Unknown historical physical recordings need
explicit ownership-ledger adoption before native deletion; they are not silently
erased or charged against unrelated objects.

The recording slice, including owner/ticket tombstones, TLS transport and
recovery-fence hardening, passed real MySQL quota transactions, real Redis
HTTP/CSRF/user deletion and the complete Linux race suite in the isolated
development checkout. The original ten-node topology is unchanged.

The active upstream can now rename a native Follower directory through the
authenticated storage-control endpoint. Durable ownership markers/journals
recover filesystem moves and uncertain SQL commits; replay preserves IDs,
statistics, visibility and completed upload accounting paths without changing
capacity. Live reservations and unresolved deletes block rename. A Python
Master's body without operation_id remains accepted. Native HTTP, audit rollback,
lost-acknowledgement recovery and post-rename deletion/refund checks passed local
tests, real MySQL, Redis integration and the complete Linux race suite.

Master directory rename now uses a durable multi-owner coordinator. Both old
and new namespaces are fenced before physical moves; affected placements are
hidden until all exact object destinations are verified. Offline/uncertain
owners retain their paths and capacity, and bounded background retry rolls
forward. Local ownership markers survive interruption; a separate SQL cleanup
phase remains discoverable after a committed-but-unacknowledged reply. Catalog,
metadata, completed upload paths and success audit commit atomically without
changing quota. Mixed Primary/Direct/Relay HTTP, simulated legacy lost replies,
restart, audit rollback, interrupted source markers, promotion races and
post-rename deletion/refund tests passed local Go/vet checks, real MySQL/Redis
integration and the full Linux race suite (2026-10-02).

The coordinator reuses the mutation ledger with explicitly typed native rename
states/manifests, preserving the shared logical table set. The old Python delete
recovery reader cannot interpret those records: pending operations must finish
and completed native rename manifests must be drained by native maintenance
before a Python rollback. That maintenance/rollback workflow is still a gate;
no original node or database has been switched to the new runtime.

Master Admin attachments now stream remote Direct/Relay bytes through the
authenticated Master instead of redirecting the browser or requiring a local
copy. ZIP selection spans Primary/Direct/Relay placements, deduplicates overlaps
and uses stored entries with bounded buffers. Each remote response is pinned to
object/resource/owner IDs over the verified TLS data plane; byte length, EOF and
available native SHA-256 proofs are checked before releasing the final block.
Offline failures before bytes return retryable JSON, and interrupted archives
never receive a successful ZIP footer. Real Redis Admin authentication and
mixed-owner HTTP download tests, private-CA transport tests, full Go/vet checks,
real MySQL regression and the complete Linux race suite passed (2026-10-02).

Native Follower backup begin/chunk/commit/abort handlers now enforce verified
HTTPS, exact-body HMAC/nonces and a fresh active-upstream/role check inside each
SQL write. Standard-base64 chunks remain at most 192 KiB; commit streams ordered
chunks through SHA-256 without a payload-sized buffer. Identical lost-response
chunk/commit retries are safe, conflicting bytes are rejected, ready artifacts
cannot be reset by begin, and the latest-generation pointer cannot regress.
Retention keeps the newest two ready generations. Abort only cleans that paired
Master's receiving generations; success state and audit are atomic. Nanosecond
generation precision, binary/empty chunks, concurrent replay, sequence/checksum
failures, role/revocation fences and audit rollback passed SQLite/MySQL, signed
native HTTP, full Go/vet and the complete Linux race suite (2026-10-02).
The MySQL fault injection uses a temporary CHECK in the disposable test database;
no SUPER privilege or binary-log security relaxation is required.

That reception slice does not establish safe recovery. The native Master still
needs artifact validation and safe restore/promotion. The v2 transfer contract is documented in
`protocol/v2/backup-transfer.md`; this does not silently introduce a new recovery
format or prove Python/Go live backup interoperability.

Native Master export now reads the fixed v2 business tables from one repeatable
SQLite/MySQL snapshot and normalizes datetime/integer values at the persistence
boundary. Lyric bytes are pinned by the cross-process shared mutation lease:
public reads continue, but physical publication/rename/delete cannot race the
copy. Unsupported pending intents/reservations defer export instead of silently
dropping recovery state. Private bounded-memory JSONL artifacts have a complete
footer and SHA-256 before transfer; ordinary success/failure/cancellation removes
them. Identity keys, pairing credentials and cold backups are not exported.

A separate OS-leased serial backup worker now builds and sends bounded signed
chunks without holding the media lease or heartbeat connection pool. It uses
the existing daily/five-minute retry intervals, rechecks the current enabled,
active pinned downstream before messages, and joins shutdown. Only an exact
ready byte receipt advances the Master's verified pointer and success audit.
Lost begin/chunk replies clean receiving data; a lost commit reply preserves the
remote ready generation without claiming a local acknowledgement.

Cross-table snapshot consistency passed on real MySQL and SQLite. Native signed
HTTP transfer, lost replies, live-reservation deferral, revocation, parallel
heartbeat/media access, shared snapshot leases, worker shutdown and private
artifact cleanup passed local Go/vet, real Redis regression and the complete
Linux race suite (2026-10-02). The compatibility artifact and remaining recovery
limits are documented in `protocol/v2/business-backup.md`. Safe restore/promotion,
legacy ownership adoption, interrupted-process cache maintenance and actual
mixed-runtime backup interoperability remain acceptance gates.

Native read-only recovery preflight now inspects exact ready cold generations
from a repeatable SQLite/MySQL snapshot, surviving concurrent retention without
mixing manifests or chunks. It rechecks sequence, bounded chunk bytes, totals
and SHA-256 at EOF; prefix reads, ignored stream errors and out-of-band corrupt
SQL payloads cannot produce proof. This is local maintenance infrastructure,
not an online cold-backup read endpoint or restored relationship authority.

The native `verify-backup` command checks a file with an explicit expected hash
or a configured cold Master/generation without initializing the authoritative
store. Untrusted rows are staged in a private disk-backed SQLite scratch child
using the embedded shared schema. Strict framing/duplicate-key/schema/type,
integer/datetime, generated-column and unique/SQL-constraint checks precede
cross-row media/lyric/recording ownership and capacity validation. Unsafe lyric
paths, malformed hash encodings, unresolved reservations and inconsistent
references fail closed; ordinary failure/cancellation removes the exact scratch
child. Reports never expose row data and always state `restore_ready=false`.
This does not establish portable restore, physical ledger adoption or authority
to overwrite, remap relationships or promote a node.

Cold snapshot retention races and corruption checks passed on real MySQL and
SQLite; real-driver export and native signed delivery artifacts passed the new
preflight. Strict malformed-input/capacity/reference tests, read-only CLI role
preservation, scratch cleanup, full Go/vet checks, real Redis HTTP regression
and the complete Linux race suite passed (2026-10-02). The Python contract
oracle also passed 567 tests (4 skipped). All original ten development nodes
remain running unchanged; neither this report nor those tests establish final
mixed-runtime recovery or an only-Go deployment.

This is NOT a complete backend replacement. Existing Master/Follower identities
are explicitly refused at startup until the cluster implementation is complete.
The default deployment and updater still contain Python and must be replaced
only after the remaining acceptance gates pass. The original test topology has
not been redeployed.
