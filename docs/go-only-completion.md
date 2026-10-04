# Go-only backend completion gate

The default backend is native Go, not a Python wrapper. The explicitly selected
Python reference remains independently maintained for interoperability; it must
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
- [x] Complete environment configuration and selected database deployment
- [x] Public pages, catalog, streaming, playback, lyrics and brand
- [x] Admin authentication, CSRF, keys, tree, upload, visibility and deletion
- [x] Directory rename/priorities, lyric management and security observability
- [x] Node identity, fixed role, pair/confirm/revoke/re-pair and heartbeat
- [x] Master global catalog, storage allocations and Direct/Relay capabilities
- [x] Recording accounts, quotas, upload/finalize/download/delete
- [x] Logical business export, mixed transfer and independent read-only preflight
- [ ] Portable physical restore/promotion and historical cluster migration admission
- [x] Native standalone updater, exact release proof, maintenance, rollback and cleanup
- [x] Actual mixed-profile whole-release execution/convergence
- [x] Only-Go default deployment contains no Python process or Python-dependent script
- [x] Four isolated 1 Master / 3 Direct / 6 Relay private-CA integration validations

Do not switch the existing development topology to Go until the replacement
passes its integration gates. Do not claim completion from schema tests alone.

## Latest verification (2026-10-03)

At production slice `8f8f2f5`, four sequential fresh private-CA fleets passed
`TestRealMixedRuntimeFleetControl` in 1531.04 seconds: Go/SQLite 346.22s,
Go/MySQL 344.66s, Python/SQLite 418.61s and Python/MySQL 421.54s. Every fleet
used one Master, three Direct and six Relay Followers cycling both runtimes
and both stores. No original node, volume or database was redeployed.

Actual pairing/confirmation/configuration/heartbeat, revoke/re-pair, placement
across all ten physical stores, public global catalog and exact byte ranges,
account registration, ten recording uploads/finalizations/ranges/deletions,
CSRF/anonymous rejection, cold transfer to Python/MySQL and Go/SQLite Followers
and independent native read-only logical preflight passed. A real native
Follower stop became offline and ineligible for storage; restart restored its
fixed role and heartbeat. All ten Web processes, Redis AOF and MySQL then
restarted with node identities, relationships, account session and recordings
preserved. All ten media deletions and catalog invalidation converged.

The root deployment now selects Go/SQLite; explicit Python/SQLite and both
MySQL overlays passed actual Compose parsing. Immutable operator bootstrap
archives only an exact commit's fixed native paths. Its capture-only shell
regression rejects branch/short/missing selections and excludes mutable edits,
untracked files, tracked secrets/data and the reference runtime. That regression
is not itself actual image-build evidence. The actual operator archive builder
then built all three native images from a fresh private Git fixture; root
SQLite and root+MySQL Compose projects passed non-root native PID checks, absence
of Python in Web/updater, absence of the Docker CLI in the updater, revision /
runtime labels and non-root control-socket access. Redis / database / Web
restart retained the sealed identity receipt. SQLite remained mode 0600 with
no MySQL service, and MySQL created no authoritative SQLite file. The private
HTTP fixture overrides only container listener-family selection so its random
host ports stay on loopback. It follows the established root-page redirect.
At production slice `436ec59`, a second fresh-image four-fleet run passed in
1574.00 seconds: Go/SQLite 349.74s, Go/MySQL 348.78s, Python/SQLite 443.05s and
Python/MySQL 432.42s. In every fleet a stopped native Follower was launched with
TLS disabled: the real process exited with the fixed-role HTTPS rejection,
retained its sealed provenance, and recovered the same identity/heartbeat after
TLS was restored. Both actual native standalone upgrade/handoff/rollback stacks
then passed again (SQLite 266.09s, MySQL 284.03s). Actual `COMPOSE_FILE` selection
for all four profiles passed independently; the latest local Python suite passed
589 tests with four skips, alongside full Go tests/vet. Operator bootstrap and
these release stacks use private Git fixtures, not published production CI.

Reproduction on a disposable Linux host:

```bash
bash scripts/test-go-business.sh
bash scripts/test-go-deployment.sh
FRONTIERCLOUD_REVISION="$(git rev-parse HEAD)" bash scripts/test-native-default.sh
bash scripts/test-go-updater.sh
bash scripts/test-mixed-runtime.sh
bash scripts/test-mixed-release.sh
```

Do not select the original deployment's project, data, secrets or database for
these fixtures. The default profile is Go-only; the optional reference profile
is not removed from the repository or silently invoked by native services.

Historical Python Master/Follower admission remains fail-closed without native
provenance. Logical preflight continues to report `restore_ready=false`.
The remaining unchecked items above are not covered by the passing core fleet;
default Go startup is not permission to overwrite historical state. Chronological
slice notes below describe their evidence and limitations at the recorded dates.

## Native whole-manifest wire revalidation (2026-10-04)

Mixed whole-release integration initially exposed two real execution defects.
A fresh ten-node fleet completed
common 1.0 confirmation and actual 2.0 replacement through the real Master Admin
endpoint and signed Follower control after fixing transient agent socket gaps.
Rollback then exposed a reference SDK global-image inventory race: four peers
failed inspecting the same retired foreign image although their target images
were present and locally committed. Inventory is now filtered by full owner and
optional cleanup failure retains images without failing a healthy generation.
These fixes, shared-Engine tag isolation and fixture proxy-subnet discovery have
regressions and now have successful serial real dual-direction acceptance.
The serial rerun at `/tmp/fc-mixed-verify-f252b47a91384620a5364f072cc5f3c7.log`
passed in 2272.17 seconds: Go/SQLite Master 1454.77s and Python/MySQL Master
817.39s. Each fresh private-CA fleet had three Direct and six Relay Followers
cycling both runtimes and stores. Both directions passed common 1.0 confirmation,
actual 2.0 replacement and rollback to 1.0. Every peer proved the common whole
digest, its privately selected artifact and the live/compiled agent SHA. Identity,
relationships, account sessions, recordings and exact media ranges survived the
release cycle. Outage/HTTPS-role recovery, all Web/Redis/MySQL restarts and all-owner
recording/media deletion passed afterwards. The driver exited 0 after private
fixture retirement; it did not reuse original volumes or issue original DB writes.
Publication proof uses unchanged fixed HTTPS verification with a private-CA
synthetic CI/review fixture for real private Git objects, not public production CI.
The native build stage now contains its Docker recipes so compilation-budget
contract tests also run during immutable image builds. Both agent and operator
archive allowlists include those exact committed recipes, not working-tree files.
Earlier standalone release proof below predates these changes. Failed release
maintenance was retained, and original development nodes were not redeployed.

### Shared development host resource incident (2026-10-04)

Running three acceptance drivers concurrently exhausted the 14 GiB host. Kernel
records confirmed OOM kills. Original Master, Direct-1, Relay-1 and Relay-5 MySQL
processes automatically restarted once; all ten original Web processes remained
running without restart. A subsequent read-only inspection found every original
Web and MySQL healthy. This health check is not proof of business-data integrity.
No original service was explicitly replaced, and no original database mutation
was issued by the acceptance drivers. The incident was nevertheless an impact
on the original environment and must not be described as "untouched".

Only the identified disposable drivers, 58 owned test containers and 13 empty
owned networks were stopped/retired. Their data and images were retained; no
global prune, volume deletion or original-project cleanup was performed.
Native image compilation now uses build-only `GOMAXPROCS=2` and `-p=1` without
changing runtime defaults. Further full acceptance drivers run serially on this
host, with memory and original-service health checked between runs.

A fresh actual native stack exposed a missing control-socket branch: direct
daemon/executor tests could accept whole manifests, while the compiled Unix
wire handler still rejected them. The handler now admits two disjoint strict
start forms, selecting whole-manifest artifacts from its durable local policy.
Actual socket regressions cover both profiles, nested duplicate keys, unknown
fields, null/malformed manifests, mixed manifest/SHA selection, default versus
explicit maintenance flags, exact acknowledgment and durable worker delivery.

The disposable SQLite and MySQL whole-manifest stacks then passed in 248.69s
and 279.19s. Legacy single-SHA upgrade/handoff/rollback passed again in 352.76s
and 262.64s respectively. The compiled
agent used its unchanged fixed HTTPS publication verifier against an isolated
private-CA publication fixture. The fixture supplied synthetic reviewed CI
metadata for actual private Git objects; it is not production CI evidence.
No host resolver, public trust store or verifier authority setting was changed.
An encoded manifest with the wrong reviewed tree failed before replacement and
retained the old whole history. A joint release changing only the other profile
advanced/rolled back whole history without replacing native containers. Native
artifact upgrade, immutable agent handoff and rollback retained exact manifests.
The complete actual four-stack driver exited successfully; local full Go
tests/vet and the Python suite (589 tests, four skips) also passed. The expanded
native CI driver runs both wire forms on both stores under a 65-minute job limit.

That earlier standalone proof alone did not establish Python artifact execution
or ten-node convergence; the later serial dual-direction run above does.
Portable physical restore/admission remains a separate, unchecked future gate.

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

Native offline maintenance now persists a fail-closed local-volume fence before
draining Go processes. Initializers, migrations, startup recovery, admitted HTTP
handlers and all four existing background loops retain their lifecycle lease
through cleanup and resource closure. Operator `enter/status/resume` commands
inspect existing schema/role and unresolved intents without erasing them or
granting restore authority. SQLite inspection is read-only and cannot initialize
an absent file; all reports still state `restore_ready=false`.

The Nginx hard fence defeats UI force-open and Admin/control exemptions while
retaining only the fixed read-only error page. Native local quiescence is not
proof that legacy Python or remote SQL writers stopped. The scope and failure
contract are documented in `protocol/v2/native-maintenance.md`. Existing
`rename_done` manifests still require validation and drainage before Python
rollback; this maintenance slice does not establish safe restore or adoption.
The backup preflight video path was also corrected to the existing `vido` root,
with a regression rejecting the unsupported `movies` root.

Persistent timeout/crash fences, ignored-context HTTP cleanup, failed reopening,
initializer/migration admission, root/operator ownership and read-only SQLite
inspection passed local Go/vet and full Linux race checks. Real Redis-backed
native server subprocesses passed two start/drain/resume cycles, with maintenance
refusing subsequent startup before initialization. Logical diagnostics passed
real MySQL and SQLite without deleting pending intents. Actual Nginx routing
passed force-open/Admin/control/health/static rejection and fixed error-page
rendering. The final source passed the isolated real-driver/race suite, and the
Python contract oracle passed 567 tests (4 skipped), on 2026-10-02. The original
ten nodes remained up for four days and were not redeployed.

Native Admin node management now uses existing session/CSRF authentication and
verified HTTPS for promotion, pairing, transport mode, storage/backup settings,
revocation and identity reset. The joined control worker wakes for changes.
Reset rotates the sealed signing identity atomically, retains revoked credentials
and cold backup history, and refuses live global placements, reservations,
recordings and unsafe physical state. Cached role/owner reads now use the fresh
database identity. Real MySQL/Redis, Linux race and Nginx tests passed.

The offline `adopt-storage` command supplies exact-size publication receipts for
already registered Follower audio/video after complete physical verification.
It requires the persistent closed native fence, refuses unknown bytes/staging,
preserves IDs and quota, and rechecks current relationship/identity in the audit
transaction. Audit failure rolls back receipts; replay does not recharge storage.
Native deletion refuses missing historical receipts instead of refunding an
unproven file. Recording adoption and all-writer isolation remain separate gates.

This is NOT a complete backend replacement. Existing Master/Follower identities
are explicitly refused at startup until the cluster implementation is complete.
The default deployment and updater still contain Python and must be replaced
only after the remaining acceptance gates pass. The original test topology has
not been redeployed.

Completed native rename history now has an explicit offline, physically fenced
and audited drainage command. It refuses pending/unknown state and retains the
original success audit, bytes, IDs and quota. Strict JSON validation tolerates
MySQL's canonical object formatting without accepting duplicate/unknown fields.
Follower recording owner cleanup now finishes durable deleted tombstones rather
than leaving maintenance permanently blocked by terminal work. These changes
passed real MySQL/Redis, Linux race and Nginx regression on 2026-10-02.

Native release Admin and signed Follower control handlers now preserve session,
CSRF, HTTPS, HMAC and replay contracts. GitHub verification requires the exact
merged production commit, reviewed source tree and newest matching successful
push workflow; older success cannot override a newer failure. Bounded cache,
rate-limit backoff, fresh identity/relationship checks and pre-queue audit faults
are covered, including real Redis HTTP and Linux race tests.

The native Docker updater now builds immutable, allowlisted Git archives and
replaces exact project services through the local Engine API without Python or
the Docker CLI. Durable queue/replacement/self-handoff journals preserve failed
release maintenance across crashes. Native offline preparation preserves schema
generation and identities. A bounded Master coordinator requires fresh pinned
relationships and exact follower acknowledgements before declaring convergence.
Completed or failed restart recovery rechecks real service images and readiness;
manual reopening is audited and cannot defeat native offline maintenance.

A disposable Linux Standalone stack passed actual image builds, native secrets/
media initialization, non-root control socket access, upgrade, Go updater image
handoff and rollback. Separate actual Engine tests passed replacement, health,
helper failure and mount preservation. Latest recovery, strict flag ownership,
Admin maintenance/CSRF/audit tests passed real Redis and Linux race checks on
2026-10-02. See `protocol/v2/native-updater.md` for the precise boundary. These
checks do not prove mixed-cluster releases, portable restore or final deployment;
the existing ten-node topology and the startup cluster-role fence are unchanged.

## Native candidate verification (2026-10-03)

Separate Gin/SQLite and optional Gin/MySQL Compose candidates now use native
initializers, UID/GID 10001 Web and a separately restricted updater. SQLite has
no MySQL service/dependency; MySQL uses the same logical schema and creates no
authoritative SQLite database. Actual isolated stacks passed native upgrade,
compiled updater handoff and rollback for both stores. The root deployment is
still unchanged pending final acceptance. Actual Compose JSON checks cover
commands, permissions, immutable revision labels and project selection.

Native database selection is now bound durably to the data root before opening
the first writer, with existing-vault identity proof before legacy admission.
An accidental `.env` switch cannot create an empty database next to an existing
identity. The fixed-target backup-cache maintenance command requires a closed
fence, matching identity and committed audit before deleting only owner-marked,
inactive native leftovers. Unknown/legacy files and symlinks remain untouched.

Node observations and four storage capacity facts now reflect real SQL heartbeat/
membership state. Temporary playback diagnostics preserve bounded process memory,
sensitive-key filtering, signed relay/fallback and retirement. These additions,
cache crash/lease tests and selected-store admission passed Windows checks and
the isolated real MySQL/Redis, Linux race, native drain and Nginx regression.
Metrics/documentation and self-handoff failure recovery passed the real-driver
and Linux race suite. Both actual SQLite and MySQL stacks then passed native
upgrade, immutable updater handoff and rollback again. Production HTTP route
registration now has shared-route and OpenAPI coverage through the actual Gin
factory. Protocol-v2 capability negotiation uses shared Python/Go vectors;
legacy peers retain only the established baseline, not new feature capabilities.
The latest route/capability slice passed the cross-language Linux rerun, actual
Compose parsing and Python/Go x SQLite/MySQL initialization, restart and shared
generation migration checks. The Python oracle passed 572 tests (4 skipped).

Historical recording adoption now requires a short-lived inventory signed by
the current Master, complete exact-size/SHA-256 physical verification and a
transactional recheck of the current pinned upstream. It preserves already
charged capacity and IDs, refuses unknown files, pending work and tombstones,
and publishes ownership receipts atomically with audit. The command chain,
replay, deletion/refund, audit rollback and scan failure tests pass locally;
the new adoption slice also passed the real MySQL/Linux race rerun, including
the full native command chain and physical authority-change faults.

The bounded whole-release manifest now has shared Python/Go parsing/digest
vectors, privately selected artifacts, durable queue/current/previous joint
history and independent exact CI proof. Historical proof cannot reuse HEAD cache
or let an older success supersede the newest failed source run. Authenticated
Follower RPC forwards the whole manifest, never a Master-selected artifact SHA.
Nine-peer mixed-profile tests require both common digest and local artifact
convergence; status RPC advertises the feature only through a supporting agent.
The general identity/heartbeat baseline still does not advertise it.

Both Master implementations can opt into a read-only publication metadata file
through `RELEASE_MANIFEST_PATH`; Admin request bodies cannot provide a target or
manifest. Both profile proofs and current production HEADs are required before
upgrade queueing; rollback uses only durable whole-release history. Membership,
pins and authority are rechecked around proof/probes. Failed source metadata or
CI cannot reach Docker/source mutation. See `protocol/v2/release-manifest.md`.
These controls pass local regression, not real fleet release acceptance. The
latest full Python oracle passed 581 tests (4 skipped), alongside Go tests/vet.

The final gates still include runtime-agnostic mixed releases, portable restore/
physical ownership, default deployment promotion and isolated ten-node mixed
startup/restart acceptance. Existing production Master/Follower startup remains
fenced, and the original ten-node environment has not been redeployed.
