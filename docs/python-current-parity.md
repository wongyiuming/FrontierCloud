# FrontierCloud Python feature renewal

This ledger records implementation and acceptance; it is not release proof.
The owner requested another Python parity release on 2026-10-19, followed by
native GitHub archival. The owner subsequently authorized publication on a
positive automatic review, without a separate S3 experience gate. The later
address-only Admin presentation change received owner experience approval before
its push in `580e894`. Archival remains a separate repository action after all release gates.
Only the existing `dev` and `main` branches may be used. Production is not a fixture.

## Hosted CI boundary correction, 2026-10-23

The owner requires the Gin CI boundary: bounded source/policy/frontend checks
and Compose configuration rendering on GitHub; complete unit, database,
browser, certificate, migration-hour and recovery acceptance on the development
host before release. Hosted test jobs are parallel, limited to three minutes,
and run bounded 150-second commands. They do not prove native acceptance.
The old hosted native failures and earlier CI topology below remain historical
evidence; moving tests does not turn those failed runs into successful runs.
Current CI, exact-source development acceptance, review and same-tree promotion
must all pass independently before archival. Owner experience approved the
address layout before its push in `580e894`.

## Pre-promotion evidence checkpoint, 2026-10-22

The states below record this checkpoint, rather than live release status.
The published branch, exact-head CI and promotion record determine the release.

PR 75 promotes the public Python `dev` branch to `main`; it does not modify Gin.
The first public candidate, `854dcade`, receives three actionable review findings:
eager catalog tag reads, alternate AWS bucket endpoint ownership, and duplicate
Master migration schedulers. Follow-up `a1a80ddf` corrects those paths and the
non-root installer test fixtures without relaxing the product's root-only lock
or identity ownership checks. Its exact seventeen-file scope passes 180 focused
Python cases, two Node regressions and four source policies. Complete CI and
native acceptance are separate requirements, not implied by those components.

Review of `a1a80ddf` finds three further issues: missing remote ID3v1 fallback,
music-specific labels in shared video catalogs, and metadata starvation without
`requestIdleCallback`. An independent check also reproduces overlapping bucket
ownership through an AWS bucket PrivateLink alias. Follow-up `a524a291` corrects
all four. Its ten changed runtime/test paths pass 81 focused Python cases and
three Node scripts; the complete submitted tree passes all four source policies.
Automatic review of that commit completes without findings, with the bot's
positive reaction recorded at `2026-10-22T04:19:32Z`. Complete exact-final-source
CI and native acceptance remain separate gates. The retained full V6 native run
fails on a test driver's low-traffic handling; V7 passes its focused repair only.
Neither is represented as a successful complete final-source run. Actual public
SQLite/MySQL many-small and catalog-budget supplements retain their own source
identities and results; unfinished runs are not passing evidence.

The owner's address-only UI is deployed privately for experience and remains
outside the public commits recorded here. It places the address between file
size and encryption, without NodeID or node-name duplication. Development access uses
normal certificate trust. Production and the Gin repository remain untouched.

## Earlier pre-publication checkpoint, 2026-10-21

The V4 runtime candidate passes 1,900 discovered unit entries: 1,882 execute
and eighteen require their dedicated database environment. Its exact content
SHA-256 is `6535b5baf248605d5b9c0dc4f495543c57cebb8a39bafa5b8b485b7f5064ba69`.
The separate exact-source S3/operator MySQL component passes 52 cases without
skips. The owner preview now uses this runtime, preserving all 57 original table
projections, eleven S3 bindings, identity and keys. Its verified 2,313,115-byte
state backup excludes media and recordings. Actual Windows Chrome opens and
plays the original encrypted album with normal certificate trust. This is not
the owner's final upload/download acceptance; the owner selected "later".

The complete V4 native run ends unsuccessfully at the fleet startup gate.
Verified HTTPS reaches Nginx, but Docker resolves the shared `web` service name
through the fleet network rather than the trusted private network. Admin login
correctly refuses that forwarded request with 426. The interrupted background
DR cleanup hour is not counted as passed. All six fleet cleanup steps complete;
the independent ended-source census finds no associated host processes or
containers. The original nonzero result and exact inputs remain retained.

Subsequent changes affect tests, CI evidence and this ledger only. A disposable
private-network alias removes the ambiguous upstream without widening proxy
trust or bypassing TLS. Five construction regressions and the related cleanup,
input-collector and S3 fixture regressions pass: 36 pure local cases in total.
New native checks require fresh business Range and browser decryption after
Source retirement, and actual provider credential revocation followed by safe
recovery. Those new native scenarios have not yet passed. CI collects the
specified safe receipts and actual operator argv while retaining raw diagnostics,
request files and credentials privately. The new exact source still requires
complete native acceptance, public CI and review before promotion. No release
or rearchival occurs before owner S3 acceptance.

### Earlier candidate checkpoints

The second complete native run, private source `22308ee8`, is deliberately
stopped after an independent S3 review proves that a definite multipart-create
refusal can strand its reservation. The recorded exit is 124 because the outer
GNU timeout receives SIGTERM, not because six hours elapse. Its four unit shards
are unfinished. An exact-source census finds no remaining runner or container;
the owner preview and production are untouched. V14 retains V1's original failed
run and V2's exact four-file test/document difference, not successful V2 native
acceptance. Its archive is 378,880 bytes, SHA-256
`3baea52dc30d4dee15251f8bea22db478179a15a33f0751f718c36a50d811622`.

The S3 correction distinguishes a verified, identity-consistent, allowlisted
client rejection from an unknown outcome, and separately recognizes local
credential expiry before any HTTP call. Only an unchanged `creating` journal
with no provider ID, grant, checksum or output marker may return to `reserved`.
It retains quota, encryption metadata and the original deadline until ordinary
cancellation or expiry releases them. Unacknowledged creation, timeout, 5xx,
malformed or conflicting responses retain their existing reconciliation guard.
An initial focused candidate passes all 88 S3 cases and actual verified-provider
refusal, restart cancellation, exact resource release and retry. The focused
wrapper later fails its English-comment policy because that disposable source
has no Git metadata; the original nonzero wrapper result is retained. A further
red regression proves that a legitimate exact bucket-only `Resource` must also
be accepted. The resulting exact V3 snapshot passes 126 focused cases and 49
actual MySQL cases without skips. Its full native run never starts: the next
cross-module check finds that legitimate S3 `aborted` history is rejected by
the operator's generic `complete/cancelled` idle rule. V15 retains these scopes
and the still-open issue at that checkpoint; its archive is 1,280,000 bytes,
SHA-256 `42cb7d109df675b3500c849c933a87db4dd875b88df8b883b932e024d378f6d8`.

The subsequent correction preserves all historical rows. Offline inspection
recognizes only a matching local-Master S3 abort with settled grants, no object
ownership or live descriptor, and the original released shared reservation.
Retained part audit rows, a normally removed member and a new object at the same
logical path remain valid. Unknown aborts, conflicting identities, active grants
and remaining reservations still refuse. Four actual SQL regressions pass after
the original API-generated cancellation reproduces the failure. The new complete
source, actual-provider/operator sequence and MySQL cross-module checks retain
their separate gates. Neither earlier focused sweeps nor unchanged-file proofs
are relabelled to cover later runtime edits. Owner S3 acceptance remains pending
after the owner selected "later"; at that checkpoint the preview update is not
yet applied.

### Earlier component checkpoints

The private 709-file `final-native-source-v1` snapshot passes all 1,880
discovered unit entries: 1,862 execute and 18 explicitly require their dedicated
MySQL environment, with no failures or errors. Its content SHA-256 is
`58017afeb9a05764c950b627367b9716637b6874e1d229eae4b14ba31ee03341`.
Both databases also pass the separate real-HTTPS 1,074-object migration
component, with 1,024 objects in one complete parent, bounded enrollment,
Target restart and per-parent cutover. All original bytes, IDs and encryption
descriptors survive. It stops at the unchanged cleanup grace, without claiming
the actual cleanup hour. A fixture TLS connection delays Source-first shutdown;
the Target-first control passes within the original ten-second limit. Both
earlier failed runs remain retained; this is not a production shutdown promise.

The real Redis/SQL catalog component passes 140 checks on each database.
Ordinary category, subcategory and track warm reads do not repeat directory
scanning or directory-priority sorting; fixed migration/S3 namespace queries
remain accounted for. Authenticated priority, hide/unhide and rename mutations
invalidate previously warm catalogs. Fresh opaque karaoke handles are resolved
to the same media identity; URLs and every other business field remain compared.
The unit/many-small/catalog evidence archive V13 is 70,420,480 bytes, SHA-256
`46204211115b474feb9f7c4dc26b24330dc1ec63284bf110db1ae49ae77e8463`.

The first complete native run passes its source/JavaScript gates, native
transfer/cancellation, certificate checks and nineteen fleet/browser stages,
then fails before the Nodes browser business checks because Docker's default
subnet pool is exhausted. The interrupted background hour is not counted as
complete. Two ended historical Python S3 fixtures are identified by their
original manifests, exact project labels, stopped containers and private bind
paths; their containers and empty networks are removed while every bind target
is retained. Gin, the owner preview, daemon configuration and production remain
untouched. Nodes/fleet cleanup reporting now counts failed client, container,
network and temporary-directory cleanup as failed acceptance, preserves earlier
business exceptions and handles SIGTERM through cleanup. Twenty focused pure
regressions pass; these later test changes require a new exact-source run.
Owner final S3 acceptance
remains pending after the owner selected "later". Nothing is published or
rearchived on the strength of these components.

The 707-file `upload-transport-fixed-source-v4` snapshot passes all 1,866
discovered cases with no failures/errors and 18 explicit dedicated-environment
skips. Its content SHA-256 is
`c26f6ccccbe6765c9e6d6f5344cfc344459f9373dfec6c1f64c44d5226ab190f`.
The subsequent lyric/template and native-runner changes below are not relabelled
as covered by that sweep.

The new real-browser presentation gate exposes an inherited override:
`lyrics-window.css`, loaded after `player.css`, restores nowrap/hidden overflow
and fixed inline row heights. The shared stylesheet now wraps without changing
font sizes; seven-line animation advances by the actual departing row height.
The shared assets receive content fingerprints on Audio/Karaoke pages. Eighteen
focused cases and the 17-check trusted-Chromium presentation component pass,
including mobile long-title navigation, inline/fullscreen glyph containment,
unchanged computed font sizes and failed cache-only artwork with continuing
actual audio. The fixture does not prove a saved user lyric relation or browser
restart caching. Earlier failed runs remain evidence.

The owner development preview is updated from generation 6 to 9 using frozen
`upload-transport-fixed-source-v7` images. Native offline preparation and fenced
verification preserve all 55 original table projections, identity and keys.
Its verified state-only backup is 2,016,143 bytes, excluding media/recordings;
eleven existing S3 objects remain, with no reserved capacity. A read-only preview
Nginx template initially refuses startup when its entrypoint tries in-place
editing. Explicitly preserving the template's maintenance include restores the
development NAT entry; no product guard or certificate check is disabled.
Windows Chrome opens and plays the original encrypted S3 album and manually
switches tracks. This fixture update is not public operator-upgrade proof.
Owner final S3 acceptance and complete final-source gates remain pending.

The subsequent presentation V11 component passes 25 checks. It closes a
persistent-profile Chromium process completely, restarts it with normal NSS
trust and proves the same immutable music Logo uses browser cache/304 evidence;
the first lifetime downloads its payload normally. Both lifetimes decode the
actual image. This proves Logo reuse, not persistent encrypted-media caching.
All three one-core/512 MiB Web fixtures pass CPU recovery and have zero OOM or
memory-limit events. Windows Chrome also reaches later tracks in the preserved
S3 album without another track selection; the observation ends with an explicit
pause and no media error.

Native cleanup failures now turn Player/Five/FS/S3 receipts red and fail the
process, while retaining an earlier business exception. SIGTERM enters owned
cleanup. The complete runner joins all background tests before cold operator
discovery and also runs the successful-cleanup actual hour, including all
Target byte hashes, three-sided fence release and both completion receipts.
Thirteen pure cleanup/ordering regressions pass; actual full execution of this
new orchestration remains a separate gate.

The `remote-tags-fixed-source-v3` snapshot passes complete four-shard discovery:
1,866 cases, zero failures/errors and 18 explicit dedicated-environment skips.
Its 700-file content SHA-256 is
`e39c81726ccdfae302e62c353c035df23872b2bc1becf564977bbc59faf41778`.
The actual MySQL batch finishes all 29 modules and records 505 checks. Its
post-test collector then fails because it assumes the last unittest output is
JSON; a separate verification retains that original failure, verifies every
module result and confirms owned fixture cleanup. Another 23 S3 object/Admin
SQL cases pass against actual MySQL. These SQL results are not TLS proof.

The subsequent 703-file `paired-storage-source-v1` snapshot also passes all
1,866 cases with 18 dedicated-environment skips and no failures/errors. Its
content SHA-256 is
`fa37c5da60751c5e5dc1736235436ad28fb0ec281775dbe2b1881a0c3c23617d`.
Later native-fixture changes and the upload-transport repair below require
their own final-source gates.

The Local/Direct/Relay player component passes 46 checks across six separate
plain/encrypted albums, each with a track above 128 MiB and a distinct next
track. Manual switching, seek-to-real-tail automatic switching and original
embedded picture hashes/dimensions pass. The run finds and fixes remote plain
artwork's catalog ETag lookup (`payload.etag`, rather than a nonexistent outer
field); two actual SQL/ASGI regressions and 61 focused checks protect it. It
does not claim hours of uninterrupted playback or persistent browser caching.
All three Web containers have one CPU/512 MiB and no OOM. Compared with a
separate empty paired fixture, final process RSS rises about 4–6 MiB. File
cache accounts for much of the cgroup use; Master reaches its memory limit
with 240 pressure events. Neither peak process RSS nor absence of memory
pressure is claimed.

The S3 V8 MySQL/provider/browser profile passes 59 checks in 760.051 seconds.
It waits 300.585 actual seconds: the original read URL returns 403, a new grant
reads and decrypts an uncached range, and MSE seeking/automatic next continue.
Its final CPU samples are 3.88, 0.83, 0.90, 0.72 and 0.93 percent. The separate
V4 result retains actual thirty-minute idle-upload cleanup; V8 does not repeat
that wait. A later regression exposes missing selected/visible metadata loading
for plain S3 songs. Both player predicates now admit the explicit
`browser_metadata` flag; Node regressions pass. V9/V10 Chromium prove each title
and original picture but fail the request-budget assertion: provisional headers
omit Range after a redirect. The V11 full-header/input-and-response observation
passes all 66 checks in 820.405 seconds, including separate plain S3 titles and
original images, actual grant expiry, deletion and restart settlement. Its five
final CPU samples are 7.13, 0.79, 0.67, 0.72 and 0.79 percent. Earlier failures
remain retained.

The first complete GUI migration/Nginx-reader hour profile fails its final
observation: the serial scheduler correctly waits on the earliest parent,
whereas the fixture watches only the parent supplying the held reader. Source
and Target bytes remain retained. The corrected fixture watches all parents
and verifies the actual socket after capability expiry. V2 passes 38 checks,
including complete task release and independent GUI retirement after 3,679.161
seconds in the terminal phase. It retains 1,252 GUI input records including
status polling, plus the separately recorded retirement inputs. The real reader
has read 2,576,384 bytes at controlled close, has neither EOF nor a socket error,
and has no live durable playback lease. All three one-core/512 MiB Web fixtures
recover below the CPU threshold. Product deadlines and reader guards are unchanged.

Native orchestration overlaps independent actual-hour waits with unrelated
fixture work, with mandatory joins before success. The initial concurrent
MySQL/operator experiment makes the cold-recovery container census refuse
payload movement; those phases are now serial, retaining the safety check. Two
actual Docker cleanup-smoke cases exercise the scripts' own cleanup functions,
retain an unrelated sentinel and preserve the triggering exit code. Thirty-seven
policy/deployment cases pass. This does not establish a successful complete
concurrent workflow. Later browser, runner and documentation changes still
require final-source acceptance. Every PY01–PY18 row below remains in scope.

The combined component archive V8 is 4,392,960 bytes, SHA-256
`5f96955e662894617894f890a550baf46bf74d24ebc7c4e1d737ee1fa5124420`.
Its immutable inputs include the earlier failures. SQLite fixed-Master cold
recovery also passes; the first MySQL counterpart refuses payload movement while
concurrent fixtures change Docker inventory. Its serial rerun passes in 470.192
seconds with identity/session/media preservation and new writes after recovery.
Actual paired-Follower upgrade/recovery profiles were still pending at that checkpoint. No source or result
from these components is represented as a reviewed public release.

Paired Direct V5 subsequently passes in 307.626 seconds: actual published old
Master/Follower pairing and uploads, Follower generation 4 to 9 upgrade, a fresh
post-operation heartbeat, original Range/full-download bytes, retained inode
and new uploads. The independent test CAs are trusted normally. Earlier fixture
failures retain exclusive-file creation, full-download versus Range selection,
strict proxy admission and null Compose-network evidence. Relay and paired
cold-recovery cases were still required at that checkpoint. The later three-case
V6 component passes in 1,097.644 seconds: paired Relay forward upgrade and paired
Direct/Relay cold recovery, including original public Range/full-download bytes,
retained payload inodes, post-operation authenticated heartbeats and new uploads.
These are unpublished development components, not public-source release proof.

Five-node traffic V2 passes 24 checks with actual Nginx byte observations,
two ongoing Relay downloads, manual pause retained through Master restart,
manual resume into traffic pause, sustained-load refusal and automatic recovery
after the observed quiet interval. All five one-core/512 MiB Web fixtures pass
CPU recovery and no-OOM checks. It ends at cleanup grace and does not repeat the
actual hour. The first fixture correctly refuses reducing a used allocation
to zero; the rerun selects Direct placement through existing transport modes
while preserving all allocations.

The native filesystem fault profile proves actual incomplete receiver bytes
and same-path reservation refusal, but its first offline case does not settle
within 90 seconds. A separate red browser-transport regression proves that raw
XHR abort has no rejection handler. Shared native upload handling now settles
abort/offline/error, resets an inactivity watchdog only on increasing byte
progress, and clears listeners/timers on every outcome. Status exposes the
existing inactivity setting; continuous uploads have no new total-time cap.
Cancellation and optional S3 status refresh share a ten-second abort budget;
unavailable replies retain backend recovery authority. The Node transport/Admin/
S3 smoke checks and 58 focused Python checks pass. The six real filesystem
browser interruptions and current S3 results follow below; complete final-source
gates remain pending.

The repaired filesystem V4 profile now passes 38 actual browser/Compose checks.
Six Local/Direct/Relay plain/encrypted interruptions each leave 262,144 actual
receiver bytes before interruption. Plain network loss reaches a visible failed
result; encrypted page refresh interrupts the real request. Same-path admission
is refused. After actual Master restart the last reserved object is reconciled
by the unchanged worker in 300.332 seconds, without a manual cleanup call or
deadline rewrite. Reservations and known parts disappear; three previously
published files retain their hashes, usage and Range reads. All three capped
Web fixtures have zero OOM and memory-limit events. Initial CPU samples include
short spikes; five subsequent consecutive samples satisfy the recovery rule.
The updated S3 MySQL/provider/browser V12 also passes 66 checks in 819.231
seconds, with five low CPU samples following an initial 23.20 percent sample.
It does not repeat the thirty-minute idle wait. The complete native runner now
includes the five-node and filesystem profiles and enables that actual S3 idle
wait. Final-source full execution remains required.

The later terminal/artwork archive V9 is 3,133,440 bytes, SHA-256
`d35979001c455a95b568931f2948b0aad9f0c2d475874e6dc15879979b7c4897`.
It includes the actual-hour GUI result, S3 V11 result, exact inputs and retained
first failures. Subsequent MySQL recovery and paired profiles have separate records.

The unit/paired archive V10 is 4,167,680 bytes, SHA-256
`1049af7e93d2500f9dddb11e80ad5d85e82afbeb279d52998d801793b19ed0e3`.
It retains the 703-file unit sweep, successful serial MySQL recovery and paired
Direct upgrade, plus their exact inputs and the failed first five-node and
filesystem fixtures. It predates the upload-transport runtime repair.

The owner selected Gin's per-parent copy/verify/commit pipeline on 2026-10-21.
PY05 therefore includes complete metadata enrollment before copying and genuine
mixed committed plus incomplete/never-started cancellation. Earlier whole-task
copy-barrier results remain historical evidence, not proof of this new gate.
The V10 private pipeline snapshot passes 21 focused SQLite cases and five actual
three-database MySQL cases, including bounded metadata enrollment, one-parent
copy/verify/commit sequencing, scheduler reconstruction, native publication
refusal and two cancellation boundaries. Actual HTTPS, an independent Master
process restart and mixed-partial cancellation settlement are not claimed.
The earlier 56-case journal/scheduler/routing sweep and private unlink primitives
retain their exact source scopes in the [migration contract](python-resource-migration.md).
The later internal mixed-partial candidate passes four focused cases per database
and all eight trusted-TLS Direct/Relay/database configurations (24 checks each,
2,327 actual HTTP inputs), including independent Target process restart and the
same persisted receipt after reply loss. Legacy Target/unit paths pass 38 cases;
history/routing/ordinary completion pass 33. Whole-task completion in the focused
cases uses virtual cleanup time. The TLS component stops before the real hour;
independent Master process recovery, actual-hour mixed-partial terminal release,
complete final-source gates and Admin integration were open at that snapshot.

The next independent-Master component passes 25 trusted-TLS checks in 82.583
seconds: a new controller process retains the Master identity, takes runner
epoch 5 to 6, and recovers the original native receipt. It deliberately stops
before the real cleanup deadline. Four processes share one CPU/512 MiB during
recovery; no OOM occurs, but raw memory-limit and socket-pressure events remain
part of the evidence. The first two actual elapsed-hour mixed-partial runs reach
the unchanged one-hour deadline with the Source socket still active, but then
time out while awaiting the expected reader-wait transition. They fail acceptance
and do not establish terminal release. The cause is the nonretryable duplicated
`migration_waiting_waiting_reader` code. Its corrected state mapping and a genuine
virtual-reader completion case pass. Recovery also authenticates an old-epoch
Target proof before obtaining fresh current-epoch authority. Actual-hour reruns
are in progress; no deadline or product guard is relaxed.

The subsequent V17-source extensions pass 44 checks on each database with 2,837
actual HTTP inputs in total. SQLite takes 3,715.528 seconds and MySQL 3,724.684
seconds. The original one-hour deadline elapses with an actual Source socket
still open and no live capability explaining its protection. Cleanup waits for
that reader; closing it and waiting for the quiet interval permits a new Master
process to finish the same task. Committed Source replicas are removed, retained
uncommitted originals remain intact, owned controls are released and all three
readonly histories pass. Each complete parent/peer/controller group shares one
CPU/512 MiB. There is no OOM, but memory-limit/socket-pressure events are retained
and are not described as absent. These are Direct-to-Relay component results on
their recorded source, not the complete final-source transport/fleet gate.

The independent Admin module and application scheduler lifecycle now have a
candidate implementation. Eight actual SQL/service-transport API cases pass on
both SQLite and MySQL; the SQLite source also passes seven module-order and eight
Admin transport/audit cases (23 total). Two real Chromium viewport cases retain
28 browser inputs using explicitly labelled fixture API responses. They prove
UI behavior, not actual backend/TLS or fleet acceptance. The current-source full
unit sweep at that snapshot runs 1,838 cases with two obsolete assertion failures
and three event-loop lifecycle errors. The module-count and inconsistent-copy
expectations are corrected; joined lifecycle recreation fixes the loop errors.
The relevant 19 focused checks and 29 startup/role/lifecycle checks pass, while a
new complete exact-source run remains required. Final-source native/browser/fleet gates and all other
ledger rows remain required before release. The first actual Compose/GUI migration
fixture first identified an encrypted-upload integration bug: ordinary Direct
uploads keep the descriptor at the Master, while planning assumed it was also
stored at Source. The repaired V13 trusted Chromium/Compose component passes
22 checks and retains 21 actual GUI requests: ordinary encrypted upload, read-only
plan, two-parent Direct-to-Relay cutover, identical logical IDs/catalog/ciphertext,
actual browser decryption and constrained-CPU recovery. It stops before Source
cleanup and does not claim terminal completion. Later SQL/history refinements,
the exact-final-source full suite and fleet matrix remain required. The two
authorities and backward-compatible immutable object shape are recorded in the
[migration contract](python-resource-migration.md).

The later immutable-object compatibility refinement keeps earlier task grants
byte-for-byte in their old shape during partial-copy recovery. New plans bind
native Source metadata explicitly; removing or changing that binding cannot
reinterpret an encrypted task as an old object. Seven focused SQLite checks and
eight actual three-store MySQL checks pass, including unchanged staged ciphertext,
no decryption, native quota retention and mixed cancellation/history. Their cleanup
clocks are declared virtual. The earlier 1,853-case full sweep has zero errors,
one now-corrected staged-verification assertion failure and 18 dedicated-environment
skips. A newer complete sweep is still running; no focused result replaces it.

The V21-source certificate baseline also passes actual test-CA HTTP-01 issuance
and renewal through Nginx, retained-stream reload, Nginx recreation and verified
Direct/Relay native TLS plus signed pairing. It establishes no public CA issuance
or low-CPU concurrent-status/error-classification acceptance; PY09 remains open.

The later certificate-control V5 passes 54 focused cases, including all eight
lifecycle states, shared in-flight reads, cancellation isolation, fresh subsequent
observations, nonce-bound independent downloads and a real six-second total
deadline. Typed RPC/CA failures exclude private paths and response bodies. Its
native lifecycle component repeats real test-CA issuance/renewal and Nginx
recreation, then passes Direct/Relay steady-state and post-restart four-way status
checks: 96 caller operations retain 48 actual Unix RPC input records. The agent
remains at 0.10 CPU/128 MiB; inspected Web test fixtures use one CPU/512 MiB.
The initial 16/16 busy failures, the premature post-restart query and the missing
test memory override remain failed evidence. No success is inferred for an
unavailable observation; exact-final-source full/fleet gates remain required.

The separate current fleet component passes 19 real HTTPS/browser/business stages
and serial CPU recovery on all three one-core/512 MiB Web fixtures. It covers the
existing public playback, crypto, pairing, backup and authoritative business paths,
not the complete new migration/retirement/S3 fault matrix. Its first setup failed
on an occupied fixed subnet; the next correctly refused an Admin request through
an untrusted fixture proxy. The fixture now allocates free public/private test
subnets and trusts only its actual Nginx subnet. HTTPS checks remain enabled and
browser trust uses a disposable NSS root, without TLS-error bypass flags.

The current S3 MySQL/provider/browser component passes 49 checks in 1,897.813
seconds, including actual thirty-minute unattended partial-upload recovery.
Five final Web CPU samples are 0.86, 1.24, 1.21, 1.08 and 0.75 percent. Exact
Compose project ownership is checked before removing only its disposable
containers, volumes and networks; bind data and the separate owner preview are
retained. A new actual read-grant-expiry/long-playback profile is still running.
The added complete GUI migration/Nginx-reader/retirement hour profile and fixed
Master upgrade checks are also pending. These component runs do not establish
final-source release readiness, and all PY01-PY18 requirements remain in scope.

The subsequent complete four-shard discovery passes 1,864 cases with zero
failures/errors and 18 explicit dedicated-database skips. Coverage binds all
four shards to content SHA-256
`d8461b56fec00676c55f031eefb579682a5835cec1ade802ab2e234db5eb6a6c`
(698 files). Later edits to native fixtures, documentation and CI require their
own checks and final-source acceptance. The local combined unit/S3 evidence
archive is 4,147,200 bytes with SHA-256
`4200656154c937afe90820e19128730d9fadacba60ad05d2c30bba832d351d29`;
it preserves earlier failed Master/expiry fixtures and their actual inputs.

The later fixed-Master application component passes actual published-source
SQLite and MySQL upgrades from generation 4 to 9 in 672.340 seconds. Both retain
identity, endpoint, original media and pre-upgrade session Range access; new
post-opening uploads survive independent recovery after a lost opening reply.
The MySQL old layout also admits its missing init service. It uses disposable
test-CA image wrappers and explicitly refuses public handoff of unpublished
source. These two cases do not establish paired-Follower upgrade/recovery,
public release provenance or the complete final-source operator gate.

## Authoritative inputs

- FrontierCloud and FrontierCloud-Gin are independent projects. The published
  Gin version is a feature/design reference, not Python's implementation,
  dependency, schema, release or deployment authority. Python's architecture
  and contribution rules govern this work. Gin checkouts are read-only inputs;
  no Gin change, commit, push or deployment is part of this task.
- Python base: `5da7a21c208e5fa758ff926443d5169dd466406d`.
- Discovered published Gin reference: `5aece3eee5345f11f1c4b43ec5f80397cf482ce2`.
- Previous accepted Python reference: Gin `a6f8e3a5ec960578bc0d08b070afbddc2370243f`.
- The current Gin working directory contains unpushed Compose-layout changes.
  A separate working tree contains an unpublished deployment-package upgrade.
  Neither is silently treated as released functionality or as accepted Python scope.
- Historical Python results remain in `python-final-parity.md`; they are not
  acceptance for this new overlay.

The initial static discovery found 27 identical files, 20 changed files and
five files added by Gin. Matching files and route names do not prove behavioral
parity. Runtime-specific implementations need their own native evidence.

## Decisions reported before implementation

The owner approved these decisions on 2026-10-19:

- Use published Gin `5aece3e` as the feature/design reference, excluding its
  unpublished complete-deployment-package upgrade work.
- Use Compose plus a Python operator entry point for installation, upgrade,
  preflight, maintenance fencing, state backup and failed-upgrade recovery.
- Run Python and Gin separately. Preserve old Python data, identities and node
  relationships; do not introduce mixed clusters or database interchange.
- Reuse the small Nginx C traffic observer, compiled only during the Nginx
  image build. Preserve sendfile and measure actual CPU/memory costs.

On 2026-10-20 the owner confirmed the Python operator's execution boundary:
extend the existing host one-shot Python entry which invokes Compose. Business
containers receive no Docker socket, and no Docker-controlling operator
container or Go foc is introduced. Implementation and old-deployment recovery
acceptance remain required.

The implementation preserves Python, FastAPI, one ASGI worker, existing Python
databases and browser encryption. It introduces no Go executable. Each reused
browser asset or design still requires Python integration and native evidence.

Python source delivery cannot satisfy Gin's compiled-Web-image release proof
without a different provenance contract. A source update still needs atomic
version selection, dependency compatibility, maintenance fencing and verified
state recovery. Copying files into a live container is not evidence of those
properties. The approved Compose/Python workflow needs its own source provenance
and recovery contract rather than adopting Gin's image proofs.

The approved low-traffic migration design observes
actual ongoing Nginx bytes through its small C dynamic module. Python
previously lacked that observer. Reusing the module requires compilation only while
building Nginx, introduces no Go, and preserves sendfile. Its Python integration
and isolated measurements are recorded below; fleet scheduling remains pending.
Completed-request logs are not a substitute for
ongoing byte observations. Resource costs must be measured, not inferred from
source size.

## Complete scope and required evidence

| ID | Required behavior | Evidence required | Recorded pre-promotion evidence |
| --- | --- | --- | --- |
| PY01 | Python business/control/TLS runtime, native browser UI, single ASGI worker | Docker/Compose inspection, runtime process inspection, source/dependency guards | Existing baseline; revalidation pending |
| PY02 | Compose-centered source installation and updating with safe state transitions | Approved delivery contract, old-deployment upgrade, new-service admission and failed preparation recovery | Install/upgrade/resume/recover use verified selected source. Entry V4 passes 299 focused cases/four policies and five real Docker-host cases: four trusted-TLS source-exec install/resume profiles, real host exits after up/publication, same-release no-op and pre-build directory-conflict refusal. Earlier cases prove new init-service admission, installed-origin SQLite/MySQL recovery and a following MySQL upgrade. Fixed-Master SQLite/MySQL and paired Direct/Relay forward/cold recovery components now pass; genuine public-release proof and exact-final-source full gates remain pending |
| PY03 | SQLite and optional MySQL migrations, exact old values, identity and premaster retention | Actual old Python schema upgrades, faulted migration/retry and typed before/after records | Generation-4/5/6 signatures retained; formal generation-7 SQLite rollback/unknown-structure refusal and real MySQL implicit-DDL retry passed. Database V7 preserves original tables/keys across generation 4 to 7 and lost-reply retry. Actual published old SQLite/MySQL Standalone upgrades preserve 37/36 typed table projections and keys before opening. V21/V22 separately restore physical generation-4 state into empty replacements, open the pinned old package, verify its exact original generation/identity/keys and retain new API uploads across a lost reply. No DDL downgrade is inferred. Fixed-role Master/paired-Follower components subsequently pass through generation 9; final complete-source evidence remains required |
| PY04 | Authenticated capabilities, request limits, nonce/replay protection and durable resource identity | Actual peer API tests and adversarial identity/replay cases | Signed controls, request-bound error envelopes, short Master transfer grants and Target private-key proofs implemented. Operation-bound retirement HMAC, durable shared nonces, exact body/binding checks and revoked-credential receipt replay now pass real SQLite/MySQL TLS completion/restart cases. Actual verified-TLS copy/restart/cancellation candidate separately passed 34 checks; complete adversarial/fleet matrix remains required |
| PY05 | Whole-parent migration planning, complete physical inventory, durable reservations/jobs, direct peer bytes, verified cutover and delayed Source cleanup | Both databases and all Direct/Relay pairs; byte/identity/encryption, restart/cancellation, real grace and many-small-file cases | Per-parent enrollment/copy/cutover and mixed cancellation are implemented. Earlier eight trusted-TLS components retain their recorded source scopes. Later DR elapsed-hour components pass 44 checks per database with 2,837 HTTP inputs, independent Master/Target recovery, reader refusal, terminal release and three readonly histories. Ordinary encrypted-upload GUI passes 22 checks/21 inputs. Latest descriptor/old-object compatibility checks pass eight cases per database with virtual grace. Complete final-source transport/fleet/resource gates remain required; see [migration evidence scope](python-resource-migration.md) |
| PY06 | Actual business-byte traffic pauses heavy migration work; sustained quiet interval resumes; manual pause requires manual resume | Long active uploads/downloads, interrupted observers, restart epochs, five-node traffic and manual-pause evidence | Earlier SQLite/MySQL scheduler and fault cases remain scoped separately. Five-node V2 passes 24 real HTTPS/Nginx checks: actual Relay bytes hold heavy work, manual pause survives Master restart and quiet, manual resume under load changes to traffic pause, and observed quiet resumes automatically. It stops at cleanup grace. Final-source full database/transport/fleet gates remain required |
| PY07 | Independent retirement preflight and fresh final checks, durable fences, old credentials revoked, retained audit and no host/volume deletion | Unknown/live resource blockers, in-flight readers/writers, lost acknowledgement/restart and final authentication checks | Generation-8 journals, native barriers, selected-file census, immutable history and two-sided signed completion are implemented. Admin V7 passes 143 focused SQLite/Linux cases, 47 route/deployment cases, 57 actual MySQL file/API cases and four TLS completion/restart scenarios. Browser V8 passes 21 checks. GUI terminal V2 now also passes 38 checks through the actual cleanup hour, held Nginx Relay reader, complete task release and independent retirement. Identities and audit remain. Final-source retirement/fleet/release gates remain required; see the [retirement contract](python-node-retirement.md) |
| PY08 | Consistent state-only operator recovery, backup retention, separate business backups, no media copies or implicit DDL downgrade | Actual SQLite/MySQL backup/restore, sidecar identity, failure durability and retained managed reads | V21/V22 prove empty-target old-state restore and reopening, retained failed roots and post-opening uploads. V24 proves unfenced cancellation and original-resource recovery after backup ENOSPC on both engines, with retained incomplete archives and payload inodes. Fixed-Master SQLite and serial MySQL cold recovery now preserve identity, original-session Range reads and new uploads after lost replies; MySQL takes 470.192 seconds. Earlier concurrent inventory refusal remains failed evidence. Paired Direct/Relay cold recovery subsequently passes; final-source full acceptance remains pending |
| PY09 | Automatic Python ACME issuance, renewal, restart, safe timeout/error classes, accurate concurrent status under low CPU | Real ACME test CA, ordinary trust validation, actual serving fingerprint, interrupted renewal, CPU-constrained concurrent probes | Control V5 passes 54 focused cases and native test-CA/Nginx/Direct/Relay lifecycle checks; 96 concurrent caller operations retain 48 wire inputs before/after restart under the original 0.10 CPU/128 MiB controller limits. Busy/timeout observations are unknown, separate from CA/certificate failure. Final-source full/fleet gates remain required; see [automatic TLS](automatic-tls-python.md) |
| PY10 | Bounded coherent catalogs and invalidation after committed mutations; cleanup does not cause unnecessary rescans | Cache hit/query counts, failure/race/invalidation tests and actual catalog mutation/read checks | Migration namespace cases cover unavailable Redis notifications, failed cutover and SQL failure. Actual Redis plus SQLite/MySQL ordinary catalog profiles each pass 140 checks: warm reads avoid repeated scans/directory sorting, fixed namespace SQL is counted, and priority/hide/unhide/rename invalidate warm entries. Full-source release gates remain required |
| PY11 | Original-quality album covers, lazy visible work, browser ID3 metadata, browser HTTP cache and bounded authenticated ciphertext reuse, optional tiny player thumbnails | Actual browser plaintext/encrypted Local/Direct/Relay catalogs, cold/warm/restart requests and blocked-image playback | Six Local/Direct/Relay plain/encrypted albums pass original picture hashes/dimensions in the 46-check player component; remote plain ETag regression fixed. S3 passes separate selected/visible plain titles, original images and bounded Range observations. The 25-check presentation profile also proves public Logo reuse after a complete browser restart, failed optional artwork with continuing audio and wrapped lyrics. Exact-final-source gates remain required; ciphertext chunk cache is worker memory, not persistent browser storage |
| PY12 | Long MP3 continuous playback without whole-track size rejection, bounded memory and backpressure, current/next tags and pictures, business-parent navigation and wrapped lyrics without font reduction | Known/unknown lengths above 128 MiB, source-buffer pressure, interrupted reads, manual/automatic transitions and real browser checks | Six actual Local/Direct/Relay plain/encrypted cases pass above-128-MiB startup, manual and real-tail automatic switching; S3 repeats after actual read-grant expiry. Cgroup pressure is retained, with no OOM. The later 17-check presentation component proves wrapped inline/fullscreen lyrics without computed-font reduction. Complete interruption and final-source resource gates remain required |
| PY13 | Safe upload/download cancellation and recovery, no live writer cleanup, no stranded quota after browser refresh or network failure | Actual browser interruption, process death, partial/finalize failures, expired grants, conflicts and restart reconciliation | Filesystem V4 passes 38 real browser/Compose checks across six plain/encrypted Local/Direct/Relay interruptions, actual partial bytes, conflicts, visible failure, refresh, restart and original five-minute worker settlement. Native XHR abort/offline/inactivity and bounded control cleanup are protected by Node regressions. S3 V12 passes 66 current checks; V4 separately retains actual thirty-minute idle cleanup. Exact-final-source complete gates remain required |
| PY14 | Nodes compact summaries, local certificate placement, removed Auto UI, independent migration and safe retirement | Actual Admin browser layout, authorization and operation lifecycle | Compact lazy cards, remembered expansion, Nodes naming and certificate placement remain. Retirement Browser V8 passes 21 trusted-Chromium checks. Independent migration has real SQL/API cases and V13 trusted GUI execution with 22 checks/21 inputs, including ordinary encrypted upload and two-parent cutover. Complete final-source browser/fleet matrices remain required |
| PY15 | CSP, authenticated admin/CSRF, hidden node-location headers, browser-only ordinary decryption, deterministic priorities and no retired compute/updater | Real HTTP/browser tests, tampered ciphertext, key revocation, source/deployment guards and negative cases | Dormant compute job endpoints/helpers, heartbeat sampling and configuration writes removed; historical tables/rows and fixed disabled peer envelope retained; focused SQLite and actual MySQL preservation/refusal cases passed; complete current-source gates still running |
| PY16 | Existing media, lyrics, playback facts, karaoke, quotas, IP policy, logo and STUN retained | Complete regressions plus actual browser/federation workflows | Historical baseline was 750 cases/five skips. Private final V1 completes 1,880 unit entries, including 18 dedicated-environment skips, with zero failures/errors; actual MySQL components retain their recorded source scopes. Its fleet stage passes 19 HTTPS/browser/business checks and CPU recovery on three one-core Web fixtures, but the full run then fails at Nodes network setup. Later test changes and all remaining native/database gates require a new final-source run |
| PY17 | Complete exact-source CI and native acceptance, review, same-tree dev-to-main release, independent release/version verification, then read-only archival | Successful required checks, reviewed commit/tree, release asset hashes, fast-forwarded dev, API `archived=true` and retained branches | Public dev-to-main PR 75 is submitted. Follow-up a524a291 passes source policies and receives a positive automatic review with no findings. Complete final-source CI/native gates, same-tree promotion, normal public-source delivery proof and rearchival remain outstanding at this checkpoint |
| PY18 | Optional private S3 music/video storage configured in Nodes, direct browser bytes, original encryption boundary and durable interrupted-upload recovery | Verified TLS, SigV4, plaintext/ciphertext upload, Range playback/download, expiry/revocation, multipart failure/restart, ownership and deletion; owner-authorized release criterion | Actual MySQL/browser/provider V11 passes 66 checks, including real expired-URL refusal, renewed uncached decryption, continued long MSE/automatic next, separate plain titles/original pictures, restart and delete settlement. Separate V4 retains actual thirty-minute idle cleanup. Complete cache/fault and final-source gates remain required. The owner authorizes publication after positive automatic review; a separate owner S3 session is waived. AWS real-bucket proof remains unavailable |

No missing row is replaced by a passing narrower test. In particular, frontend
controls do not establish migration or retirement support; equal schema numbers
do not permit cross-language database/vault interchange; injected candidate
proofs do not establish public release verification.

The earlier generation-8 cancel-master-source-v4 full discovery completed 1,686
cases in 3,840.829 seconds with four errors and seventeen dedicated-environment
skips. The directory-rename connection double lacked current scalar interfaces;
the recording-crypto HTTP fixture lacked the formal retirement table. Updated
fixtures pass their 34-case repair run, with one dedicated MySQL skip. This is
not a passing full sweep of the current generation-9 source. Source CI now uses
four module-preserving shards plus independent complete-discovery verification;
each receipt binds the exact source and selected ordered case IDs, including
duplicates. Missing or unsuccessful shards fail the existing required `source`
check. Two old CI contracts which required a serial command have been updated
to protect the complete-coverage gate. The initial attempt has two obsolete
contract failures and an unfinished shard, not a passing full sweep. The next
attempt was interrupted after identifying retained completed fixture instances
in the shard runner. The runner now releases them as unittest normally does;
an executable weak-reference regression protects collection. The subsequent
1,752-case run finished all shards with one failure and eighteen dedicated
skips: the runner removed the repository root from the import path, so the
upload-claim test's spawned child could not import `app`. The original failure
is retained. The root now remains available through execution; nineteen
runner/CI checks and the actual upload process-death case pass without extending
its ten-second deadline. The 1,775-case Source-release V3 snapshot has now
completed all four shards with zero failures/errors and eighteen explicitly
skipped dedicated-database cases. Independent fresh discovery verifies every
ordered selected ID, source fingerprint and successful process exit. Its longest
shard takes 1,677.706 seconds. This is the 660-file snapshot, before the new
Target/Master release candidate; it is not final-source or native release proof.
Its inputs, complete logs, receipts and coverage are retained separately in
`unit-shards-progress-evidence-v4.json`.

Application V24's two early-abort cases passed in 430.321 seconds; its 143 focused
cases passed in 24.751 seconds and four source policies passed. ENOSPC was
injected after a real control archive, without filling the filesystem. Positive
recovery uses explicit internal native-fixture source admission. Actual public
`resume` and `recover` refuse that unpublished source before fencing; no current
public release has yet been proved. The entry now returns precise host errno
classes and recorded phases without private paths. Exact host inputs, paired
step results and source hashes are in the private
`operator-early-abort-progress-evidence.json`. These are Standalone/test-CA
results, separate from the complete exact-final-source and fixed-role gates.

Handoff V2 adds actual staged new-upgrade/resume acceptance for both engines in
383.711 seconds; its 200 focused cases pass in 22.844 seconds. Real host exec
retains the process/lock; the application cases preserve original identity,
session Range 206 and new uploads after opening. MySQL also adds the missing
init service. All 563 remote source/build-input files match, and both fixture
projects plus 16 helpers have no remaining containers/volumes. Actual inputs
and paired results are in `operator-handoff-progress-evidence.json`. Positive
staged continuation explicitly uses local-source/private-CA fixture admission;
the public default bootstrap actually refuses that unpublished source. This
is not genuine current public-release or full final-source acceptance.

Handoff V3 subsequently passed 209 focused cases in 22.752 seconds, four source
policies and both staged early-recovery cases in 443.044 seconds. `recover` and
`resume` now use the indexed source and retain original resources, payload inode,
generation 4, original-session Range 206 and new uploads across a lost reply.
ENOSPC follows a real archive write through explicit fault injection; no disk
was filled, snapshot restored or volume replaced. Public auxiliary entries
refuse unpublished fixtures. All 563 remote source/build files match; both
projects and 12 helpers leave no containers/volumes. Actual inputs/results and
the local-source/private-CA scope are in `operator-auxiliary-source-evidence.json`.

Install V4 separately passed 220 focused cases in 22.628 seconds, four source
policies and four real package builds/reloads in 32.773 seconds with no skips.
The profiles cover business SQLite/MySQL and Direct/Relay Storage. Private env
bytes remain unchanged; no business container, volume, data root, canonical
Compose copy or database is created. All 566 remote source files and four actual
build inventories/modes match. Local-source/name/port fixture admission is
explicit; default public load refuses it. Inputs, failures and scope are in
`operator-install-package-progress-evidence.json`. Fresh activation, restart,
identity/TLS and first-install-to-upgrade acceptance remain required. Repeated
upgrades previously needed terminal managed-source routing before old-entry
discovery; the fix and later evidence follow below.

Selection V2 passed 235 focused cases in 22.580 seconds, four policies and both
application cases in 521.092 seconds. The MySQL case actually adds the missing
init service, then requires a simulated incapable old entry to execute retained
source before discovery and completes another upgrade. Two declared local
revision identities use the same candidate source; this is not a genuine public
release or a second application feature change. Original keys/session Range and
new uploads survive. All 569 remote files and four actual source/build inventories
match; both projects and 23 helpers leave no containers/volumes.

Resource V3 separately passed 235 focused cases in 22.986 seconds, four policies
and four actual resource-creation cases in 76.937 seconds. Real Docker create
replies are lost, then the same token-owned volume inode is reconciled. Canonical
files publish without replacing owner inputs, private env is retained and only
owned empty unused fixture volumes are removed afterward. No Web/SQL/certificate
process starts, and the result explicitly remains installation-incomplete.
Actual inputs, one-way exec records, source boundaries and retained failures are
in `operator-selection-and-resource-progress-evidence.json`.

Start V5 subsequently passed 281 focused cases in 22.739 seconds, four policies
and four actual trusted-TLS first-start cases in 454.611 seconds. They cover
business SQLite/MySQL and Direct/Relay, lost successful Compose and current-receipt
replies, ordinary discovery and a following MySQL upgrade with original-session
Range 206. All 572 source files and five build inventories match. Four projects
and 15 helpers leave no containers/volumes/networks. Actual host inputs and 93
paired child inputs/results are indexed in `operator-install-start-progress-evidence.json`.
Local source/test-CA admission is explicit; this is not a public release, complete
fixed-role upgrade/recovery matrix or full PY01–PY18 acceptance. Public install
bootstrap was still pending at that snapshot; Entry V4 below supplies its local acceptance.

Start V6 then passed two actual installed-origin controlled recoveries in 413.008
seconds, plus 281 focused cases in 22.797 seconds and four policies. SQLite/MySQL
restore generation-7 state into empty replacements, retain failed control roots,
move media directories without copying and preserve original sessions for Range
206 after ordinary trusted HTTPS reopening. Its two local source revisions contain
the same application; this is physical state recovery, not implicit DDL rollback.
Runtime source is unchanged from V5. All 572 files and four build inventories match;
both projects and 16 helpers leave no containers/volumes/networks. The private
`operator-install-start-and-recovery-evidence.json` adds 66 paired child inputs/results
and exact host inputs. Public install handoff was still pending at V6.

Entry V4 adds the real install CLI and source handoff: 299 focused cases in 22.842
seconds, four policies and five actual Docker-host cases in 400.094 seconds.
SQLite/MySQL business and Direct/Relay installations each execute their retained
source after real host exits following successful up/publication. Repeated install
and same-release upgrade retain container IDs; business sessions read new uploads
with Range 206. A stopped foreign project in the same directory refuses before
build/pull, package or data creation. All 576 files and eight source inventories
match. Four installation projects, eight helpers and the conflict fixture leave
no containers/volumes/networks. The private `operator-install-entry-progress-evidence.json`
retains 102 paired child inputs/results, 20 actual exec inputs and 40 CLI calls.
Positive source/CA admission remains explicitly local. Genuine public proof,
fixed-role fleet upgrade/recovery and complete exact-final-source gates remain
pending; this does not complete the full Python renewal.

On 2026-10-20 the full-unit-sweep V1 source snapshot passed 1,297 unit cases in
2,166.579 seconds with ten dedicated-environment skips. Its log SHA-256 is
`7799aa3376422ca974244c419414e6614dab01c0fbe90a950a27ef4171007402`.
It precedes the subsequent full-package activation and helper-lifetime changes;
it is not exact-current-source release acceptance. Application V4 separately
passed one actual old SQLite application to canonical candidate activation in
163.432 seconds, including ordinary verified HTTPS login, real upload/Range,
generation 4 to 7 and unchanged original rows/keys/payloads under the hard fence.
It never opened business traffic and does not satisfy the remaining PY02/PY08
opening, rediscovery or failed-state restore gates.

Application V6 then passed both actual default SQLite/MySQL upgrades through
fenced activation. Application V7 passed default SQLite plus a MySQL legacy
layout missing its init service in 338.241 seconds; the candidate safely adds
that service. Both engines verify 37/36 original table projections, existing
keys and payload hashes, unchanged service IDs on duplicate activation, and
CPU recovery. A real abruptly killed host Compose client leaves a demonstrably
running owned helper; the transaction then stops and joins it without fencing
or deleting business data. Later wiring V1 passed 101 focused cases, four
policies and both native/CI collector Bash syntax checks. Exact inputs, outputs,
source hashes and failed attempts are in the private
`operator-application-progress-evidence.json`. All cases remain fenced;
complete upgrade/opening, failed-state restoration and final release acceptance
are not claimed.

Application V8 separately passed real host default rediscovery for both
generated deployments. Application V13 then passed 99 focused cases in
23.023 seconds and both actual old-to-new Standalone upgrades through normal
opening in 435.217 seconds. Each verifies the original session's HTTP 206,
injects loss of the final opening reply, performs a new real API upload and
retains it through retry and duplicate completion. Five low-CPU samples pass
for each one-worker/one-CPU/512-MiB Web. Both cases also force a real temporary
container to disappear between inventory and inspect, while preserving another
live foreign sentinel and the old Web. Default host inspection, precise
read-only Nginx identity exceptions and CLI exception classification are covered.

The V13 four source policies pass. Read-only cleanup verification confirms all
ten V8-V13 application projects have no remaining containers/volumes, and all
81 recorded helper names are absent. Inputs, outputs, original failed cases,
source hashes and exact scope are in `operator-opening-progress-evidence.json`
outside the repository. This proves the private forward transaction, not an
exposed one-command host upgrade, controlled failed-state restoration, fixed-role
fleet upgrades, public provenance or the whole renewal's release readiness.

Application V14 separately passed 106 focused cases in 21.733 seconds, both
actual SQLite/MySQL application upgrades in 520.201 seconds and four source
policies. Independent host processes now load a durable private operation index
before live discovery, reap an exact owned daemon helper left by abrupt host
death, and resume database preparation/activation. A separate opening-process
death still retains a new real API upload and the original session's Range 206.
Fenced probes recheck all original typed tables; open probes allow new writes.
The public resume entry refuses unpublished fixture source before any fence.
Its immutable inputs/source/logs/results are collected in
`operator-application-collected-v14.json` outside the repository. This is
Standalone candidate acceptance, not fixed-role/public-release/final-source
proof. Application recovery V16 separately passed 113 focused cases in 21.848
seconds, both actual SQLite/MySQL restores in 534.113 seconds and four source
policies. Recovery first retains a fenced generation-7 failure, restores old
control state into sibling/new-volume destinations, and reconciles host death
after a real no-replace payload rename. Original payload inode/hash and failed
persistent-root identities are retained; restored preflight equals the original
generation-4 facts for both engines, including an actual standalone MySQL engine
start. No recovered business Web/gateway is started or opened. V15's fixture-only
uncommitted synthetic SQL failure is retained with the V16 source and actual
inputs. Later guards and full current-source native acceptance are separate;
the restored-business opening and complete host orchestration remain pending.

Application V17 adds the current phase/volume/writer guards: 115 focused cases in
25.383 seconds and four source policies pass. Four actual application cases pass
on that runtime/test source: the SQLite/MySQL forward pair in 557.958 seconds and
the isolated recovery pair in 546.719 seconds. Read-only cleanup checks six
V16/V17 application projects, 70 recorded helper names and four recovery-volume
tokens with no remaining containers/volumes. Exact host inputs, 140 paired step
inputs/results, 559 V17 artifacts and prior failed/successful snapshots are indexed
in `operator-recovery-progress-evidence.json`. Its proof matches 551 current files;
three later documentation updates are named separately. Full fixed-role, restored
business opening, complete source/native gates and final release remain pending.

The later restored-opening candidate passes 128 focused cases in 23.465 seconds
and all four policies. Application V21 MySQL passes in 426.493 seconds; identical
source SQLite passes sequentially in V22 in 332.683 seconds. Both open trusted
HTTPS on the pinned original generation-4 package, verify the original signed
identity, preserve a new actual API upload across a killed final-reply process,
read the old sample with its original session/Range 206, and admit another
candidate preparation/upgrade operation using the restored mapping. V19's
unchanged Compose-input label failure led to stopped package recreation plus
actual binding checks before the one-way opening boundary. V21 SQLite's parallel
inventory-churn refusal occurred before upgrade/fencing; V22 uses unchanged
source and unchanged limits. V20's earlier recovery pair separately passes in
744.685 seconds. Read-only checks confirm nine fixture projects, 110 helper
names and six recovery tokens have no remaining resources; 556 remote source
files and actual build stages match their proof. Actual inputs and all prior
failures are retained in `operator-restored-opening-progress-evidence.json`.
Public new-operation orchestration, early abort before a complete snapshot,
fixed-role fleets and the full renewal's final-source checks remain open.

The finalization V5 source overlay passed 1,121 unit cases in 1,544.642 seconds,
with eight MySQL integration cases explicitly skipped in SQLite discovery.
Those eight passed separately against disposable MySQL: seven schema/DDL/retry
cases in 21.340 seconds and one historical migration smoke in 0.339 seconds.
Thirteen distinct finalization cases have both SQLite and actual MySQL evidence;
their cleanup-grace clocks are declared virtual. The first full discovery failed
on two outdated certificate-renderer test adapters; the repaired adapters retain
lazy-expansion and text-only diagnostics assertions. All failed inputs remain
with their original source identities in `finalization-progress-evidence.json`.
The genuine hour/TLS/live-reader gate passed 76 checks on its separately recorded
V3 snapshot: 3,609.3147 seconds in cleanup, an actual Source socket held beyond
capability expiry, safe cleanup only after that reader closed, verified Target
originals and both native terminal receipts before task completion. It retained
the unchanged 3,600-second grace. Its one-CPU/512-MiB cgroup peaked at 419,602,432
bytes; five final CPU samples were 0.221, 0.209, 0.234, 0.235 and 0.246 percent.
This is actual TLS/bytes, not a browser decoding or full transport-pair matrix.
Later compute-retirement and Admin metadata edits are not relabelled as V3 tests.
This overlay is uncommitted and does not establish exact-commit release readiness.

### Owner S3 preview milestone

The owner confirmed normal login and visibility of the development S3 member,
then personally uploaded eleven encrypted MP3 files through actual Windows
Chrome. The GUI reported eleven successes and zero failures. This is partial
owner evidence, not permission to archive or a complete adapter acceptance.

Actual Chrome playback initially failed closed. The separate preview setup had
created the bucket without the documented CORS rule: HTTP 206 included a correct
Content-Range but did not expose it to browser JavaScript. Adding the exact-origin
bucket CORS rule preserved all eleven object size/ETag identities. No ciphertext,
descriptor or key-validation code changed. Actual Windows Chrome then played
the first track, accepted a manual switch to the second, and naturally continued
to the third after the second ended. No seek or synthetic ended event was used.
An independent CA-verified Linux Chromium downloaded the first complete MP3;
the Windows extension did not provide a completed-download receipt, so Windows
save verification and comparison with the owner's original file remain open.

The actual Admin directory also exposed a pre-existing metadata gap: cluster
tree/search responses omitted encryption state, so the frontend incorrectly
labelled encrypted entries as plaintext. The candidate now batches descriptor
lookups for visible files and refuses inconsistent S3 descriptors instead of
claiming plaintext. It returns a boolean only, without keys or envelopes.
Both tree and search have real SQLite/actual MySQL regression coverage. The
updated independent preview preserves the original identity, all eleven object
records, configuration and mounts. Current-source full discovery is still
running; the earlier V5 full result is retained under its own source identity.

Exact private inputs, original failures, ordinary-TLS browser screenshots and
scope limitations are retained in `python-owner-preview-evidence.json` and the
separate current progress receipt. No production or Gin source was changed.

### Added S3 scope

The owner has no AWS bucket and requested a self-hosted acceptance service.
AWS S3 is managed by AWS; a self-hosted S3-compatible implementation is a
separate product. The isolated SeaweedFS fixture tests the selected standard
API surface, not the actual AWS service or its IAM implementation. The optional
adapter must retain AWS regional endpoint and SigV4 compatibility. Claims for
AWS real-bucket acceptance remain explicitly unverified without an actual bucket.
The implementation boundary and required failure cases are in `python-s3-storage.md`.

## Earlier independent-retirement runtime and preparation

These are historical intermediate snapshots. The later internal API and TLS
milestone is described in the current PY07 row and `python-node-retirement.md`.

Runtime V4 installs the pinned request gate after native startup admission.
Internal HTTP retains a shared lease through actual response delivery and joined
file I/O cancellation; queued upload/recording file claims join the same inode,
including from an independent process. Health remains available under an
exclusive gate. Only an actually issued exclusive capability can start physical
clearance. The ordinary operator fence remains outermost.

Follower startup pins the selected roots, database and matching small control
files. The bounded physical census does not open media payloads, preserves
unknown material, and rejects changed/corrupt keys, substituted inodes, busy
coordination files and links. SQLite main/WAL/SHM require actual engine-held
inodes; MySQL selection has independent real-server checks. Nested roots are
not added twice; external protected vault bytes are reported separately.

Runtime V4 passes 128 focused tests in 90.885 seconds with one explicitly
CI-only MySQL smoke skipped, four policies and shell syntax/diff checks.
The skipped smoke is not reported as a pass. The same snapshot separately passes
16 actual MySQL schema tests, 15 two-database preparation tests and 10 MySQL
physical-inspection tests. The added recording case verifies an already admitted
request returns HTTP 409 before consuming its body or creating payload files.

Two actual verified-HTTPS/HMAC races pause after authentication, persist both
SQL fences and prove final exclusion remains busy until the handler exits.
The guarded handler refuses a late stage; a deliberately unguarded test-only
handler creates unknown material that the physical census refuses and preserves.
Old ordinary credentials are rejected; new internal work returns 503 under the
exclusive gate while health succeeds. Four wrong-CA/hostname requests fail TLS.
Each case then records five consecutive one-second CPU samples below 20% of one
core; V4 samples range from 0.112% to 0.160%. These fixture races do not implement
or claim terminal retirement, old-credential receipt replay or Admin acceptance.

Exact inputs, original intermediate failures, source inventories, resource
cleanup and these scoped results are retained privately in
`retirement-runtime-progress-evidence.json`. That snapshot did not establish
terminal SQL/history checks, authenticated completion or narrow replay. The later
milestones do not relabel it as HTTP or Admin retirement acceptance.

### Earlier preparation evidence

The generation-8 candidate adds a separate retirement journal without changing
the generation-2 through 7 migration signatures. Source comparison against the
retained previous candidate and three independent Python hash seeds verifies
this boundary. SQLite rollback and MySQL committed-CREATE retry are distinct
tests; a same-named unknown table, trigger, altered check, wrong index or nonbinary
column is refused rather than silently repaired or removed.

Preparation commits the journal, relationship fence, storage disablement and
audit in one writer transaction. Repeat or lost acknowledgement keeps one
operation/start time/audit. The peer rebinds the exact identities, keys,
endpoints, transport and credential before fencing. Ordinary authentication
consults the journal even if a stale relationship row says active. New media,
recordings, configuration, pairing, legacy activation/revocation and identity
reset cannot bypass it. Other Master members and Master Local remain available.
Expired reserved uploads and retained backup generations/chunks are blockers;
retirement never cleans them to manufacture a passing result.

V5 passes 142 focused SQLite/Linux and business regressions in 137.146 seconds,
with four policies plus shell syntax/diff checks. The two recording fault cases
now use real SQLite and verify zero residual storage reservations; no guard is
mocked away. V4 passes 16 native MySQL schema cases and 14 preparation cases with two real
databases per case. The latter cover audit failure on both sides, restart/retry,
changed bindings/credentials, root-lock replacement, and preservation of unknown
files. Tests use service-level calls, not actual HTTP retirement or browser
execution. The request-lock primitive has real subprocess and fork checks; it
was not installed around runtime HTTP responses on that earlier preparation
snapshot. Its results are separate from Runtime V4 above. Terminal history,
receipt replay, native completion and Admin controls cannot be inferred from
either snapshot. No retirement execution endpoint was exposed on those snapshots.

The owner preview stays at its earlier schema/image. Neither it, production,
Gin nor the Git remote was updated. Exact private inputs, retained failures,
source hashes and scoped results are in
`retirement-preparation-progress-evidence.json`. This progress does not replace
the remaining PY01-PY18 full-source release gates.

## Current active-cancellation candidate

The candidate now stops both native peers with higher-epoch signed control,
waits for real writers and migration read leases, and reconciles private staging
one object at a time. Staging object identities and full-stat delete intents are
durable. Unknown files, changed private/base directories, replaced objects and
published material refuse cleanup; source originals remain unchanged. Both
native terminal acknowledgements precede exact Master quota/fence settlement.
Ordinary playback leases and unrelated reservations are preserved. Files are
never recursively removed or discarded by task age/window expiry.

The unpublished generation-5 signature still has fourteen tables. It now adds
staging/cleanup identity fields and task/purpose indexing for read leases.
Earlier private generation-5/6 databases stay tied to their recorded signatures;
the old published generation-4 upgrade baseline remains unchanged. New fixtures
are fresh, not silently reopened under the extended signature.

Signed application errors bind the raw request digest, route, identity,
relationship and status. Verified clients retain exact fixed error codes;
unverified bodies remain generic, and encoded control responses are refused.
Direct TLS/connect/timeout categories also survive the native error path.

The initial 28-case focused run refused both replacement files correctly but
failed two assertions because fixture replacement permissions triggered a
stricter earlier check. Those fixtures now preserve mode 0600 to test the
intended inode/delete-version guard. The later focused snapshot passed 43 cases
in 208.594 seconds. A subsequent actual disconnect exposed cancellation of the
reader's SQL cleanup. The short DB close is now shielded before lock release;
two regressions passed in 15.264 seconds, covering a cancelled cleanup scope and
an actual joined write which must finish before unlink.

The exact runtime repeat passed 34 actual TLS checks in 70.6072 seconds. It
independently verifies that a real disconnect releases both the Source read
lease and SQL connection. A signed unknown-stage error reaches Master without
releasing capacity; only after the fixture explicitly moves its own injected
marker does controlled cancellation remove known parts and settle all three
nodes. The marker and every Source original remain intact. This is private
staging cancellation, not publication or cleanup of a previously committed source.
It uses two endpoint apps and an independent receiver child in one cgroup, not
the complete separate-host Direct/Relay matrix. Its one-CPU/512-MiB cgroup peaked
at 402,116,608 bytes; final CPU samples were 0.500, 0.469, 0.506, 0.498 and 0.629
percent. Source data totalled 9,498,019 bytes and Master handled 140,027 bytes of
control bodies. These counters describe ASGI bodies, not NIC traffic.

Four real MySQL cancellation cases passed in 35.4233 seconds across separate
node databases, covering partial staging, lost unlink/audit acknowledgement,
Master settlement rollback and unrelated quota/playback readers. Their data
path uses ASGI; the TLS evidence is separate. Complete discovery passed 1,026
cases in 640.202 seconds: 1,018 passed and eight external-database cases skipped.
Source/deployment, one-core, environment, English-comment, JavaScript, diff and
shell policies passed, plus ten browser smoke files and the second compiled
crypto run. Runtime hashes matched at collection across current, TLS, full and MySQL snapshots. S3
browser test additions afterward require their own evidence. Exact inputs,
failed attempts, source proofs and sanitized artifacts are collected in private
`transfer-cancel-progress-evidence.json` beside the earlier receipts. No push,
Gin change or production operation occurred.

## Current S3 playback and Nodes layout extension

A separate S3 SQLite browser snapshot passed 25 checks in 61.726 seconds:
135,290,000-byte encrypted MP3 startup, manual switching, real-tail automatic
transition, plaintext/encrypted H.264/AAC decode and seek, and warm ciphertext
reuse with fresh authorization. The ranged music responses declared a total
Content-Length of 3,267,304 bytes; this is not measured network traffic, and
cancelled requests may deliver less. This is a seek-to-tail check, not hours of elapsed
playback, and worker memory reuse does not establish browser-restart persistence.
The actual-MySQL/provider/browser combined gate subsequently passed 43 checks
in 1,897.639 seconds. An unfinished upload survives page refresh and Web restart,
retains its reservation through the unchanged thirty-minute idle deadline and is
closed by the normal recovery worker. Quota and pending UI then settle and GUI
can disable/remove the profile. No clock, SQL deadline or task state was manually
changed. Its one-CPU/512-MiB Web returned to CPU samples of 0.58, 0.63, 0.65, 0.69
and 0.66 percent. The S3 contract records the remaining limits.

The Nodes extension passed nine actual HTTPS/browser checks on a separate fresh
Standalone/Master Compose deployment, with ordinary temporary-root trust, one
CPU and 512 MiB. Node details and pairing/initialization start collapsed; details
render on demand, refresh retains the user's expansion choice, Enter works,
the local certificate is inside its node card, and CSS uses a verified content
hash/immutable response. Final CPU samples were 0.57, 0.53, 0.74, 0.65 and 0.76
percent. The earlier eight-check snapshot and 45 relevant unit cases are retained;
its wrapper's English check failed outside a Git checkout, then passed separately
in a clone containing the exact overlay. No product check was removed.

Actual Compose rendering exposed that an empty profile array did not clear the
base MySQL profile. The explicit MySQL overlay now uses `!reset []`; five real
rendering checks passed, and the combined browser run uses that overlay without
an extra profile argument. A separate fixture failure reused an old project's
identity volume with a new data directory. The product correctly refused the
missing database. Fixture project names now hash the full absolute test directory;
old identity volumes remain untouched. Private inputs retain those failed runs.
The only leaked empty fixture network was verified empty and removed; no volume
or data was deleted. These records, media hashes and source snapshots are in
private `s3-playback-nodes-progress-evidence.json`. Latest Nodes runtime hashes
have their own source proof; they are later than the preceding transfer/full and
S3 playback snapshots. A later one-line CSS correction applies danger styling
only to explicit destructive buttons; the prior positional rule colored Expand
red. This cosmetic delta is recorded separately from browser acceptance.
Full exact-commit release acceptance remains pending.

## Earlier direct-transfer candidate

`resource_transfer_master` queues a prepared task only after fresh admission
observations, a real private write/fsync/read probe and an authenticated direct
Source/Target TLS exchange. Master issues forty-five-second, epoch/object/version/
range-bound grants. Source verifies the active upstream Master's signature and
Target private-key possession, and consumes a durable nonce before serving any
bytes. Target receives no Master relationship credential. Wrong peer identities,
changed objects, expired windows and replayed requests are refused.

Target pulls at most 4 MiB per control step with 64 KiB I/O buffers. Private
nofollow staging journals precede file creation; owned task-directory identity,
capacity and immutable bindings are checked. Matching retries resume actual file
length rather than a reported counter. One bounded SHA-256 context is reused
between steps, avoiding repeated prefix scans. A new process incrementally
rehashes existing staging bytes before continuing. Full digest/length verification
and fsync precede a verified acknowledgement. Master records verified objects
with zero committed bytes; global ownership and business references stay at Source.

Master/manual controls fence runner leases. Master and both peers check live
traffic before further copying; the complete five-node scheduling gate is still
required. Readers hold expiring SQL leases and pinned nofollow descriptors, with
explicit close paths even if streaming never starts. Expired lease reaping is
bounded and does not touch files or other nodes' leases. Quota, scopes, source
drains and unknown staging material are never discarded because a window expired.
Publication and managed source cleanup are not supplied by this copy layer;
their verified reconciliation remains required before release.

The first focused snapshot passed 33 cases in 168.758 seconds. A later actual
TLS repeat passed 22 checks in 69.5054 seconds, including a real thirty-second
startup quiet interval, a failed partial write, an independent Target process
using the same identity/SQL journal, plaintext/ciphertext verification and source
preservation. Data totalled 9,498,019 bytes; only 105,941 control-body metadata
bytes passed through Master. Its one-CPU/512-MiB cgroup peaked at 401,772,544
bytes; five whole-cgroup final CPU samples were 0.427, 0.444, 0.403, 0.495 and
0.591 percent. These are two endpoint apps plus a receiver child in one container,
not separate-host acceptance or the complete Direct/Relay matrix.

Four actual MySQL cases passed in 37.3034 seconds across three disposable node
databases, covering direct-copy journals, ciphertext, staging restart and proof
rejection. Their ASGI byte path is separate from the actual TLS gate. Earlier
failed inputs are retained: a missing test preview epoch was correctly refused,
and a later successful byte test failed fixture teardown while its controlling
HTTP pool remained open. The repeat closes that pool before stopping its child
server and exits normally. Latest complete discovery passed 1,005 cases in
513.981 seconds: 997 passed and eight external-database cases were skipped.
Source/deployment, one-core, environment, English-comment, JavaScript syntax,
diff and runner shell checks passed, plus ten browser smoke files and the second
compiled-crypto run. The actual Nginx/certificate gate also passed generated
configuration startup, real test-CA HTTP-01, verified HTTPS, renewal, a retained
stream and certificate restoration after Nginx recreation. This is not a full
Nginx migration-fleet acceptance claim. Runtime hashes match the current tree,
native TLS/full/Nginx snapshot and the actual MySQL snapshot.

Inputs, failed attempts and result hashes are in the private
`direct-transfer-progress-evidence.json` and adjacent records. These remain
uncommitted candidate results, not release or full migration proof. Ordinary
playback-reader leases, complete SQLite/MySQL Direct/Relay combinations,
whole-parent publication/cutover, active-transfer cancellation/reconciliation,
real one-hour source cleanup and independent retirement are still required.

## Earlier Master preparation candidate

The coordinator creates one durable `admitting` task under the Master mutation
fence and identity-row lock. It verifies the approved, unexpired preview and
current member facts, reserves target capacity and drains both members before
installing whole-parent scopes in bounded transactions. A deterministic unit
transcript binds every source content version. An index supports ordered bounded
unit paging; no complete directory is buffered in memory or held across a peer
request inside a SQL transaction.

Matching calls and process restart reuse the same task, preview rows and native
authority. A renewable, epoch-fenced SQL lease prevents two coordinators from
writing the same Master preparation. Native confirmations and fresh signed
registration/inventory rounds must finish before the task becomes `prepared`.
Only the exact owned native reservation is excluded from pending-operation
checks; unrelated media/recording writers remain blockers. Repeat scans reuse
one bounded spool namespace. The source drain and exact capacity record are
checked independently of progress counters.

Prepared tasks still report `writable_verified=false` and
`execution_authorized=false`, and the ordinary transfer journal refuses to claim
them. No staging write probe, direct data connection, resource publication or
ownership cutover is supplied by this layer. Target upload drain is lifted only
after all scopes are confirmed; its selected parents and reserved capacity remain
protected. Source remains drained. An explicit unstarted cancellation must obtain
both native terminal acknowledgements before releasing Master fences/quota.
If begin never arrived, native cancellation creates a bound terminal tombstone,
so a delayed begin cannot recreate a reservation. Files are never removed by this
preparation cancellation. Audit failure preserves the uncommitted local state.

The initial focused snapshot passed all fourteen new coordinator cases and
failed one older ordinary-upload test whose preparation-stage expectation changed.
That test is strengthened to require rejection until all scopes are durable,
then retain its existing physical-capacity and unrelated-writer settlement checks.
The verified-HTTPS snapshot passed eighteen actual checks, including recovery
in a new Python process after a successful native begin lost its response,
without another preview or duplicate capacity. These recorded snapshots are
intermediate evidence. The later exact runtime snapshot passed complete discovery
of 990 cases in 433.530 seconds: 982 passed and eight external-database fixture
cases were skipped. Source/deployment, one-core, environment, English-comment,
JavaScript syntax, diff and runner shell syntax checks passed. Ten browser smoke
files and the second compiled-crypto run also passed.

Its actual verified-HTTPS repeat passed eighteen checks in 8.3234 seconds with
two peer ASGI endpoints and an independent Master child process. Source plaintext
and real GCM ciphertext totalled 2,154,171 bytes; native replies sent only 57,951
metadata bytes and received 11,110. The actual cgroup had one CPU and 512 MiB.
Five quiescent parent/server-process CPU samples were 0.272, 0.307, 0.263, 0.272
and 0.366 percent. Parent harness peak RSS was 210,448 KiB; child peak is not a
separate measured quantity. This is not separate-host transfer evidence.

The predecessor's actual MySQL preparation repeat passed eight checks in 67.1258
seconds, using three separate disposable node databases, actual JSON reloads,
lost acknowledgements, failed-audit rollback, repeated final scans and an
independent Master-row writer. The latest runtime repeat passed the same eight
checks in 70.6681 seconds. Runtime hashes match the latest complete unit/source
and native HTTPS snapshot; the MySQL runner uses that exact frozen source tree.
Actual inputs, retained failed inputs and source overlays are in private
`master-admission-progress-evidence.json` and adjacent files. These are local,
uncommitted runtime results, not successful GitHub CI or release proof. Actual
direct transfer and all release gates below remain required.

## Earlier native migration preparation candidate

The extended unpublished generation-5 signature adds a separate native
reservation table. Source drain, parent scopes and target quota now persist
through process/SQL reload and age. Native controls bind the active Master and
relationship, immutable request/plan digest, source/target roles, chained parent
transcript and exact totals. Ordinary media/recording writes account for held
capacity. Missing capacity, drain or scope records refuse relevant retries and
confirmation; old counters do not create replacement authority. Unstarted
rollback checks that no transfer journal/capability/advanced unit exists, changes
only its own SQL state and retains a terminal tombstone. Audit failures preserve
all fences and reservations.

That reservation-only snapshot's complete discovery passed 974 cases in 339.577 seconds: 966 passed and
eight explicit external-database fixture cases were skipped. Deployment/source,
one-core, environment, English-comment, JavaScript syntax and diff policies
passed, along with ten browser smoke files and the second compiled-crypto run.
The earlier focused snapshot passed 65 cases in 118.634 seconds; later actual
capacity/scope consistency checks have their coverage in the complete result.

An actual verified-HTTPS fixture passed 17 checks in 6.9873 seconds: two native
ASGI endpoints, valid signed metadata controls, wrong CA/SAN rejection, nonce
replay refusal, matching retry, an independent child-process SQL reload,
identity fencing and controlled unstarted rollback. Its two real stores use
one task ID and preserve the original plaintext/ciphertext files. Received and
sent HTTP body metadata totalled 3,730 and 8,547 bytes. The actual container had
one CPU and 512 MiB. Five final parent/server-process CPU samples were 0.471,
0.410, 0.414, 0.414 and 0.548 percent. Parent harness peak RSS was 207,156 KiB;
this is not a separate measurement of each endpoint or child-process peak.

The real MySQL repeat passed 15 checks in 5.1286 seconds, including an independent
node-row writer, committed pending capacity, JSON binding reload, selected-parent
write exclusion, failed-audit rollback, idempotent preparation and exact quota
settlement. Runtime hashes match across the latest native, MySQL and complete
unit/source snapshots. Earlier failed inputs are retained: an incorrect lock
method, an undersized fixture filesystem, missing common-dispatcher State wiring
and a stale CI timeout assertion. The CI assertion now requires the documented
thirty-minute budget and the newly required native/S3 evidence entries.

Actual inputs, failures, source overlays and receipts are in the private
`peer-reservations-progress-evidence.json` and adjacent files. These remain
uncommitted local results. The native test has two endpoints in one container,
not separate-host transfer. Preparation explicitly reports no write probe and
no execution authority. Staging/direct-connectivity admission, direct source-to-target bytes,
complete-parent cutover, active-transfer restart/cancel reconciliation, the
real one-hour cleanup, independent retirement and full fleet/operator/browser/
certificate gates remain required. Nothing in this milestone is a release.

## Earlier S3 object and Admin candidate

The catalog has an explicit S3 branch, with no local-file fallback for absent
Follower relationships. Read/download grants preserve identity, original names
and permanent encrypted-content flags. Browser ZIP download and plain-S3 MP3
metadata avoid Master payload fetches. SQL catalog revisions also fence cached
views when Redis invalidation fails. Folder rename changes only logical paths.
Published deletion waits for issued grants, verifies the exact owned object,
confirms absence and atomically settles quota, encryption and business metadata.
Both the generic delete intent and the S3 recovery marker commit together, so a
crash cannot require a periodic scan of every active S3 object. Recovery has a
dedicated state/deadline index. Audit failure rolls the entire delete intent back.

The candidate node panel supplies native configuration, connection checks,
enable/disable, controlled credential recovery and removal. Upload transfer uses
one bounded part at a time and distinguishes bytes sent, storage confirmation
and durable publication. S3 control messages are bounded and secret validation
errors are redacted; session, CSRF, TLS and encrypted preparation are mandatory.
The pending page limits both selected uploads and confirmed-part aggregation.
These controls are code candidates, not an assertion that the owner has received
or accepted a working browser environment.

That S3 milestone's runtime snapshot passed complete discovery of 954 cases in
285.063 seconds: 946 passed and eight explicit database-fixture cases were skipped.
Deployment/source, one-core, environment, English-comment, JavaScript syntax
and diff policies passed, together with all ten browser smoke files and the
second compiled-crypto invocation. These are local checks of an uncommitted
overlay, not GitHub review or a clean release commit. Runtime hashes match the
focused, actual-MySQL and real-browser snapshots. Actual inputs and failures
are retained in `s3-browser-progress-evidence.json` and its source/log/input
records; later fixture and documentation edits have separate checks.

The actual Chromium/Compose repeat passed 14 checks in 49.141 seconds with
ordinary certificate trust in a disposable Linux browser profile. It did not
ignore certificate errors or change the owner's Windows certificate store.
GUI operations added, probed and enabled the S3 profile, uploaded plaintext and
browser-encrypted WAV files, and downloaded both with their original names and
exact plaintext SHA-256. Public playback and seeking passed for both. Encrypted
upload used two bounded parts. Native XHR input observation recorded the actual
Blob sizes, S3 destination and absence of CSRF/cookies; independent object and
download verification matched their sizes and content. Chromium reported zero
wire-body sizes for file-backed Blob requests, so these are input/provider/hash
proofs, not a packet-level byte counter. The actual Web limit was one CPU and
512 MiB. Its last five CPU samples were 0.62, 0.87, 0.79, 0.66 and 0.73 percent.

The browser exposed a real plaintext-download defect: the Admin snapshot used
a playback grant without attachment disposition. The shared routing now passes
the snapshot download purpose through to the S3 grant; ordinary playback stays
inline. A real-SQL HTTP regression covers both purposes. Earlier failed browser
runs, including a tooling assumption about Chromium's upload-body counter,
remain recorded rather than relabelled as success. Focused expiry/admission
coverage also rejects unusable credentials before reserving capacity or
recording an uncertain provider operation; busy claims return a safe HTTP 409.

Forty-two actual MySQL object, Admin/API and upload fault cases passed in 143.262
seconds with no skips. These use controlled provider failures; they are not a
simultaneous MySQL/S3 browser test. Later browser interruption, refresh, real
grant-deadline waiting and normal background-worker recovery have separate
source and run records and are not included in the 14-check milestone above.

Forty-six focused SQLite/API/object/fence/capacity cases passed in 51.642 seconds,
with both browser-script checks passing. Nineteen actual MySQL object and ASGI
Admin cases passed in 56.754 seconds without skips, using controlled provider
faults. The actual verified-TLS compatible-service repeat passed 27 checks plus
four child-process recovery checks in 38.1574 seconds. Its independent client
transferred 8,388,678 payload bytes; control instrumentation observed forty
metadata operations and no media GET/payload. The read/delete tests include
Range, HEAD, original filename, ciphertext preservation, live-grant deferral,
lost delete acknowledgement, background settlement and repeat deletion. This is
the service layer, not an actual browser, AWS or full-Web CPU acceptance.

The subsequent repository-native repeat passed on a new compatible-service
instance: 31 independent provider checks and four restart checks, 27 native
transaction checks plus four separate-process recovery checks, and 27 actual
browser checks in 363.026 seconds. It used the repository's pinned fixture
generator and `scripts/test-python-s3.sh`, now connected to the complete native
runner. The actual Web remained one CPU / 512 MiB; its final five CPU samples
were 0.56, 0.69, 0.75, 0.87 and 0.68 percent of one core.

The browser interrupted the second part twice after an actual confirmed 8 MiB
first part, refreshed during another real partial upload, cancelled from the
GUI, requested deletion of the two previously published objects, and restarted
Web with the same Compose mounts. New reads/publication stayed hidden; live
issued grants retained used/reserved capacity. GUI profile removal was refused.
After the real 300-second grant deadlines, the normal startup/background worker
settled both uploads as aborted and both objects as deleted, with zero used and
reserved bytes. GUI disable/removal then succeeded. The test did not edit clocks
or database deadlines. An earlier 26-check repeat passed in 368.965 seconds.

The latest browser also checks the failure panel without reloading: the caller
now waits for S3 cancellation and refreshes pending metadata afterward. This
one browser file changed after the earlier full-unit snapshot; all Python,
dependency, Nginx and Compose runtime hashes still match that snapshot. Current
source/deployment policies passed 40 focused cases, all ten browser smoke files
and the second compiled-crypto invocation. This is not a claim of a new complete
GitHub CI run on a clean release commit.

Failed browser and fresh-fixture attempts remain recorded. One browser script
initially clicked the collapsed Nodes panel after refresh; the fixture was
corrected. Fresh compatible instances initially could not load their public
certificate: the runner's restrictive umask made it root-only, while the pinned
image's entrypoint drops to UID 1000. Public certificate access was corrected;
the private key retained restricted group-readable permissions. Neither TLS
verification nor product certificate management was weakened. Actual inputs,
outputs, failures, source hashes and limits are in the private
`s3-lifecycle-progress-evidence.json` and its adjacent artifacts.

That earlier browser case did not wait for unattended idle expiry or combine
real MySQL with browser/provider. The later extension above supplies those
specific gates. Repeated long playback/seek after multiple grant expiries, the
complete video/metadata/cache fault matrix, interrupted/renewed credentials and
owner acceptance still require evidence. Exact old Python
generation-4 to the latest unpublished generation-6 native preservation and
recovery also remain required. Earlier unpublished generation-6 private fixture
databases are preserved with their source receipts; they are not silently
relabelled as installations of this current schema contract.

## Earlier S3 multipart transaction progress

The candidate service now reserves shared cluster paths and S3 quota durably,
binds immutable part checksums/lengths, verifies provider metadata and composite
SHA-256, and publishes catalog/placement/quota changes atomically. Reconciliation
retains unknown create/complete outcomes, preserves original encryption descriptors,
waits for grant expiry before cancel cleanup and refuses unknown deletion targets.
A narrow recovery-credential path requires disabled placement, cancellation of
all pending work and expired grants. A bounded background worker handles restart
and abandoned-session cleanup without coupling it to node heartbeats.

Before the worker additions, complete discovery passed 922 cases in 250.738
seconds (914 passed, eight explicit database-fixture skips), with all source
checks. Thirteen corresponding actual MySQL transaction cases passed in 61.238
seconds. An actual verified-TLS service fixture passed 17 checks
plus three separate-process checks with 8,388,678 payload bytes. Provider completion
was real; a deliberately lost acknowledgement left durable uncertainty, then a
second Python process published it exactly once. Direct plaintext Range, preserved
ciphertext/client decryption, actual part-grant expiry and verified remote abort
were checked. Product control instrumentation observed metadata requests only.
These are service-layer results, not a browser or Admin HTTP acceptance claim.

The latest worker/edge-case source passed 51 focused cases in 26.096 seconds.
It additionally retains late completion outcomes, recovers a lost delete reply
from a durable verified-output record, rotates unresolved work fairly, skips a
live control lease and refuses to cancel from an outdated expiry snapshot.
The latest full snapshot passed 928 cases in 260.382 seconds: 920 passed and eight
explicit database-fixture skips. All source/deployment, one-core, environment,
English-comment, JavaScript syntax and diff checks passed. Nineteen actual MySQL
fault/concurrency cases passed in 75.003 seconds, with no skips. The verified-TLS
service repeat passed 19 checks plus four independent child-process checks in
29.4011 seconds. The real create acknowledgement was also lost and recovered by
an exact-key multipart lookup without recreating it. Background recovery selected
the original completed transaction; actual grant expiry and abort closed another
transaction. The client/service process container and separate compatible-service
container were each limited to one CPU and 512 MiB. This is not a whole-Web
quiescence or marginal resource-cost measurement.

The native repeat differs from the full snapshot only in fixture postcondition
checks and documentation; product runtime files match. Actual inputs, source
overlays and receipts are in `s3-upload-progress-evidence.json` and adjacent
private artifacts. These earlier results predate the object/Admin candidate
above. They are retained with their original scope, not relabelled as GUI
acceptance or a current release proof.

## Earlier write guards, S3 metadata and configuration progress

The pre-generation-6 `fences-source-v5` snapshot passed complete discovery:
896 cases in 195.133 seconds, with 889 passed and seven explicit database-fixture
skips. Source/deployment, one-core, environment, English-comment, JavaScript
syntax and diff checks passed. Ten SQLite write-guard cases and nine bounded S3
metadata-control cases are included. A separate actual MySQL check passed six
write-guard/serialization checks in 2.9733 seconds. These do not prove migration
admission or execution. The initial full guard run exposed incomplete old test
fixtures; their real schemas/SQL models were corrected without a runtime bypass.

The actual verified-TLS S3 fixture then passed 31 initial and four restart checks,
including project metadata control and independent payload operations. The
provider's optional ListParts checksum omission was verified against its pinned
source. Missing final checksums still fail closed. Wrong part SHA-256 is refused
by the service; the project verifies the complete composite SHA-256.

The generation-6 encrypted member registry adds persistent namespace ownership,
quota, stale-probe rejection and configuration/identity lifecycle exclusions.
Fifty-nine focused SQLite/source-fixture cases passed in 35.527 seconds, including
one explicit MySQL skip. The actual MySQL run subsequently refused the initial
TEXT default definition before advancing the generation. Replacing the upload ID
and version ID fields with bounded strings corrected that definition. Seven actual
MySQL database cases passed in 28.253 seconds, including interrupted generation-6
DDL, index repair on retry and unchanged prior table values. The same runtime
passed six MySQL migration write-guard checks and eight S3 registry checks,
including concurrent namespace registration, retained quota and restart vault
readability. The registry fixture contacts no bucket and is not a browser test.

The `s3-members-full-source-v4` snapshot passed complete discovery: 908 cases in
240.552 seconds, with 900 passed and eight explicit database-fixture skips. All
source checks above passed. The MySQL snapshot differs only in documentation;
runtime files match. Actual inputs, failed runs, final receipts and source overlays
are collected in `write-guards-s3-registry-progress-evidence.json` and adjacent
private artifacts below. These are candidate component checks, not an integrated
release or proof of a completed S3 upload lifecycle.

The final compatible-service repeat passed 31 initial and four restart checks.
An earlier repeat found metadata readiness preceding object-data readiness;
GET returned transient `500 InternalError` before the original committed bytes
became readable with the same SHA-256. The corrected restart readiness assertion
waits for that exact retained object, records transient failures, and never
recreates it. Missing/forbidden/certificate/checksum failures are not retried as
readiness. The final run recorded one intermediate 500. Its product runtime
matches the full-unit snapshot; only test diagnostics/readiness and documentation
changed. No S3 browser/GUI or upload-recovery acceptance is implied.

## Earlier readonly-plan and S3 protocol progress

The `planning-source-v4` overlay passed complete discovery: 877 cases in
176.566 seconds, with 870 passed and seven explicit database-fixture skips.
Deployment/source policy, one-core boundary, environment, English comments,
JavaScript syntax and diff checks passed. The run uses an isolated one-CPU
container; it is not the full clean-commit native release gate. Eighteen focused
plan/roster cases passed separately in 35.832 seconds. The initial capacity
fixture's incoherent modified transcript was rejected; the corrected fixture
changes the native free-space observation before header/transcript construction.
No product consistency check or physical reserve was weakened.

An additional actual HTTPS run used two peer ASGI apps with separate node stores
and a Master planner in one 1 CPU / 512 MiB fixture. After the real startup quiet
interval, it accepted a two-object, 8,466,627-byte plain/GCM parent plan with both
inventory rounds and refused a newly occupied target parent. Wrong CA and hostname
were refused; media-key operations remained forbidden and original source bytes
matched. The whole combined fixture took 31.8311 seconds and 1.8298 CPU seconds,
with peak RSS 221,092 KiB; these are not planner-only overhead measurements.
The first native run correctly refused a 512 MiB temporary filesystem; only the
fixture tmpfs size changed to 2 GiB. The same runtime files match the complete-unit
snapshot; later test/runner/document additions have separate source proofs.
Supplemental source checks include all 430 archive files, including new files
absent from the baseline Git index. Actual inputs, failed runs and final results
are retained in `planning-s3-progress-evidence.json` and adjacent local artifacts.

The same runtime source's S3 signer passed eleven unit cases and an actual
private-service check through verified TLS. Its presigned upload matched the
independent AWS SDK signature, uploaded a Unicode/percent-containing key,
supported a signed metadata HEAD and a Range GET, and refused overwrite.
Together with the infrastructure cases, 26 initial checks and restart persistence
passed. This primitive does not register a node, authorize users, manage durable
uploads or expose an Admin GUI. Those features remain required.

## Initial player work

The shared browser implementation is copied from the exact published Gin commit,
including browser crypto source and matching compiled assets. Python page loading
adds content-versioned business navigation and music-metadata scripts through
the existing template wrapper. Ordinary media decryption stays in the browser.
The old total-track limit is removed from the continuous MP3 path, while the
decoder no-progress budget and ordinary speculative preload budget remain bounded.

The complete source/native runners now include the corresponding Node regressions.
The old source assertion requiring `MAX_TRACK_BYTES` was replaced with assertions
for a bounded decoder no-progress window; it does not weaken the executable
large-track and backpressure tests.

Local executable results so far: player cache, continuous stream, interrupted
fetch retry, ID3/thumbnail behavior, business-parent gestures, browser crypto
source and compiled crypto all passed. These seven runs are not real browser,
public-network, whole-project native or final release acceptance.

The unchanged Python baseline passed complete unit discovery on the development
host: 750 cases in 62.861 seconds, five explicit skips. The first attempted fixture
used an undersized 512 MiB tmpfs, causing correct storage refusals and an upload
test to wait for a writer that could not start. That run was stopped and retained
as failure evidence. The successful rerun uses the original 2 GiB filesystem
capacity with a 1 CPU / 1 GiB container memory limit and a bounded test timeout.
This correction changes test infrastructure, not product capacity admission.

The current player candidate subsequently passed full Python unit discovery:
752 total, 747 passed and five explicitly skipped MySQL cases, in 62.635 seconds.
The additional tests exercise actual Python template injection/idempotence and
source/compiled browser-crypto manifest consistency. Seven current-source Node
runs also passed. MySQL must run against its disposable server later; those five
skips do not count as passing database integration. Migration, retirement, full
native TLS/federation/browser acceptance and the final release remain unfinished.

## Evidence custody

### Source-content observation candidate

Registered native media IDs and current-Master recording namespaces now have a
single joined, expiring content worker. It waits for low business traffic,
hashes in 64 KiB chunks with fresh relationship checks, and rechecks native and
registered metadata before returning a completed proof. Pending/failed/cancelled
states provide no usable hash result. Media premaster/key operations are absent;
recording footers retain the existing 2 MiB bound and opaque lyric ciphertext.
Metadata versions, raw content hashes, identities, selector, source incarnation
and fixed deadlines are validated by the signed HTTPS client. Full semantics
and remaining planner requirements are in `python-resource-migration.md`.

The focused control/observation run passed 34 of 35 cases in 12.432 seconds,
with one explicit legacy CI-only MySQL skip. Complete candidate discovery then
passed 848 cases in 140.120 seconds: 841 passed and seven database-fixture cases
explicitly skipped. All source checks passed. Cases cover same-length source
rewrites, registered metadata changes, revocation between reads, competing jobs,
fixed expiry, cancellation during actual file reads, no mutation lease held
while waiting, invalid recording footers, symlinks/hardlinks, retired encryption,
unfinished proof refusal and tampered signed replies. An existing opener leak
when fstat failed after opening a file was also fixed and covered by an actual
fault-injection descriptor check. Existing Range delivery regressions passed.

Actual certificate-verified HTTPS acceptance additionally hashed 8,388,611 bytes
of registered plaintext media, 78,016 bytes of real GCM ciphertext and a 524,623
byte recording with an encrypted lyric footer. Each returned SHA-256 matched
the independently retained source bytes; none of the files changed. Guards
that fail on media-premaster access or file-key derivation stayed enabled during
these calls. The same case observed 1,074 native entries and refused both wrong
CA and wrong DNS SAN. All observations closed. The final single-core/512 MiB
fixture took 31.714 wall seconds, 1.5135 CPU seconds and peak RSS 215,444 KiB.
These figures include the test client, server, source generation, database,
business imports and assertions; they are not incremental observer overhead.

Inputs, exact overlays and outputs are collected in
`observation-progress-evidence.json` and adjacent local records. All results
remain uncommitted candidate evidence. Planner catalog reconciliation, persisted
bindings, fresh admission, source-to-target transfer, publication/cleanup,
independent retirement, operator delivery and complete release acceptance are
still required. A completed content observation grants no write or delete right.

### Native inventory candidate

The Follower now supplies fixed-root, bounded metadata inventories through its
current Master's authenticated control relationship. Pages have signed process,
node, relationship, request, scope and session bindings, fixed expiry, cumulative
counts/bytes and rolling transcript proofs. The Master verifies each page before
passing it to its observation consumer. The walker never opens file contents or
follows links; changed directories/roots, malformed paths and excessive depth
invalidate the session. Queued/running I/O joins on cancellation; expiration and
application shutdown close descriptors without removing files. Full contracts
and the remaining planner responsibilities are in `python-resource-migration.md`.

Forty focused cases ran in 3.438 seconds: 39 passed and the existing CI-only
MySQL smoke case explicitly skipped. Complete unit discovery passed 834 cases
in 130.408 seconds: 827 passed and seven database-fixture cases explicitly
skipped. Source/deployment, CPU-boundary, environment, English-comment, browser
JavaScript syntax and whitespace checks passed. Cases include 1,074 actual
filesystem entries, Unicode path/page budgets, symlinks/FIFO/hard links,
directory mutation, missing roots, replay/revocation, wrong peer proofs,
transcript tampering, failed consumers, fixed deadlines and shutdown/cancellation.

An additional isolated 1 CPU / 512 MiB container ran the actual Python control
HTTPS client against the actual ASGI inventory routes over verified TLS. A
private fixture CA and DNS SAN were checked normally; wrong roots and a wrong
hostname were independently refused. After the real startup quiet interval,
1,074 entries (8,592 bytes) were observed with HMAC nonce authentication and
node signatures. Source bytes remained unchanged and the session closed. The
combined client/TLS server/database/assertion fixture used 0.7979 CPU seconds
over 30.759 wall seconds and peak RSS 108,524 KiB. These are whole-fixture costs,
not observer-only overhead or representative media-transfer throughput.

Inputs, exact overlay hashes and outputs are in `inventory-progress-evidence.json`
and adjacent local records. The new verified HTTPS case is wired into the native
runner and sanitized CI artifacts. Metadata inventory completion does not prove
file content, catalog reconciliation, a ready plan, admission, transfer or
ownership cutover. Those requirements and the full release remain unfinished.

### Durable migration foundation candidate

Generation 5 adds thirteen migration journal tables, with no rewrite of older
business or identity tables. The task journal fences workers with durable
epochs, expiring leases and task revisions. Pause/cancel invalidates the old
runner, manual pauses cannot automatically resume, and automatic resume reads
the actual traffic monitor. Committed progress must match whole-parent unit
records; unverified objects cannot enter commit. Cancellation requests retain
capacity until storage reconciliation. No peer transfer or resource cutover
is implied by these journal methods. The implementation and remaining domain
steps are detailed in `python-resource-migration.md`.

The complete candidate unit suite passed 805 cases in 127.341 seconds: 798
passed and seven explicitly skipped database-fixture cases. All source checks
passed. The actual disposable MySQL 8.4.11 fixture separately passed six cases
in 29.919 seconds with no skips, including interrupted implicit DDL, missing
index repair, old-row preservation, concurrent worker claims and control fences.
Separate process hash seeds proved the new migration signature deterministic.
Earlier failed runs are retained, including historical expectation updates and
adaptation of the transaction-order fake to SQLAlchemy inspection; actual
SQLite/MySQL structure validation was not bypassed in product code.

An additional isolated fixture used immutable Python source commit
`5da7a21c208e5fa758ff926443d5169dd466406d` to create and populate its actual
generation-4 SQLite database. The candidate upgraded that same database to 5.
There were 37 original tables and 50 after upgrade. Typed row proofs for all
35 original tables outside the generation/history markers matched exactly;
five original media/key files matched their byte hashes. The original node ID,
both Direct/Relay relationship credentials and media premaster remained usable.
The test fixture decrypted the original ciphertext with that same premaster;
no product-side media decryption was introduced. An initial fixture path outside
the admitted persistent directory was correctly rejected before initialization;
the corrected fixture retained the product admission rule.

These are uncommitted source-overlay results, not release acceptance. Full
inventory/admission, direct peer transfer, physical publication, grace-period
cleanup, independent retirement, operator delivery, Admin controls and complete
native browser/fleet/recovery gates remain required. Inputs, failures, final
logs and exact overlay hashes are collected in `migration-progress-evidence.json`
and adjacent local records. Gin remains outside the modification scope.

### Live traffic observation candidate

The Python monitor uses fifty 100 ms buckets, a five-second average plus a
one-second burst window, a 256 KiB/s high threshold, and thirty continuous
seconds at or below 64 KiB/s before resuming. Gateway epochs, stale samples,
duplicate/invalid observations and monotonic-clock rollback fail closed. The
single joined poller reads at most 4096 bytes over the fixed Unix socket;
observations contain no paths, credentials, object identities or payloads.
ASGI counts accepted receive/send body chunks, preserving backpressure. Proxy
observations use the maximum of the two layers rather than double-counting.
The operator maintenance fence remains the outermost application middleware.

The actual Python Nginx Dockerfile built the approved C module from a
hash-verified Nginx source release in a separate builder stage. The final image
keeps the existing Python TLS edge, without a Go executable or build toolchain.
Compose shares its read-only counter socket with business Web; native Storage
uses ASGI observations and does not acquire an Nginx dependency.

Full candidate unit discovery passed 788 cases in 64.236 seconds: 783 passed and
five real-MySQL-only cases explicitly skipped. Source/deployment, one-core,
environment, English-comment and JavaScript syntax checks passed. The first
run exposed an executor-policy violation and middleware placement breaking the
outermost maintenance-fence contract. Both were fixed without weakening checks.
Bounded local tag reads now use the existing file-I/O approach, with descriptor
ownership and joined completion even when the request and its helper task are
both cancelled; queued cancellation closes its unused duplicate descriptor.

Two isolated actual Nginx runs each transferred a 2 MiB sendfile response and a
2 MiB proxied upload. Counters changed before transfer completion; the download
SHA-256 matched; the Unix handler returned 403 when intentionally exposed over
TCP. The second run recovered low-traffic permission after 34.860 actual seconds
following the last transfer. Twenty-one Docker resource samples showed peak
CPU 0.44% of one core and peak memory 3.398 MiB for the Nginx fixture. This is
whole-fixture consumption, not a matched measurement of the observer's marginal
overhead. Python observer plus HTTP test clients/receiver consumed 0.2028 CPU
seconds over 41.134 wall seconds, with peak RSS 40,224 KiB; this too is combined
fixture consumption. A native fixture mountpoint error after the successful
second observer run was retained; correcting that mountpoint let all 26 focused
traffic/metadata cases pass against the same source snapshot in 0.279 seconds.

These results prove the observation path, not durable migration jobs, traffic
scheduling integration, five-node acceptance, full MySQL/browser/TLS gates or
release readiness. Actual inputs, output logs and source overlay hashes are in
`traffic-progress-evidence.json` and its adjacent records in the local evidence
folder below. The native traffic test is now part of the complete native runner
and its sanitized receipts are included in CI artifact collection.

The metadata/artwork integration passed 26 focused Python cases in 0.611 seconds
on the development host. The runner's trailing label incorrectly says "full
unit discovery"; its recorded invocation explicitly names four test modules,
so this result is focused coverage only. It is not full discovery or native
acceptance. Failed first-run evidence is retained separately.

Local discovery and source-hash records are under
`C:\Users\wyium\.codex\tmp\frontiercloud-python-alignment-20261019\`.
The development-only baseline/candidate directories are under
`/home/wyium/frontiercloud-python-alignment-20261019/` on `192.168.6.201`.
The baseline uses the unmodified public Python commit. Candidate test snapshots
record their source overlay separately; they must not be relabelled as clean
committed release sources. Private recovery data, CA keys and credentials must
never become public assets.
