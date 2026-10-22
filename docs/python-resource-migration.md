# Python resource migration implementation contract

This document defines the Python resource-migration implementation and acceptance
contract. It is not an operator runbook. Gin is a design reference only;
the repositories have independent schema, identity and release contracts.

## Approved per-parent pipeline alignment

The owner selected the published Gin execution model on 2026-10-21: register
the complete admitted metadata roster at Source and Target, then copy, verify,
publish and commit one parent before starting the next. Earlier Python records
that require every task object to have a verified copy describe the earlier
candidate, not this accepted target. Their passing tests are not relabelled as
pipeline acceptance. `Enrollment` pages one signed object registration per step;
metadata registration neither reads payloads nor claims copied/verified bytes.
Publication now requires the selected complete parent's proofs; committed units
remain immutable while the next parent can resume copying.

Registration counters are committed with each new binding under the same native
identity transaction. An older internal journal reconciles missing counters
before inserting another object. New runners retain the completed enrollment
proof and do not recount the complete task on every parent or metadata page.
The selected parent's actual verified objects, native publication roster and
atomic catalog cutover remain separate mandatory checks.

The new pipeline and internal committed/partial/never-copied unit cancellation
are implemented candidates. A later independent-Master recovery component has
passed; actual elapsed-hour mixed-partial completion and exact-source final
gates remain in progress. Public/Admin controls are now a separate candidate.
Unreconciled paths retain reservations and originals rather
than manufacturing successful copy or deletion receipts. No production or owner
preview change is authorized by this private implementation work.

The private-copy unlink primitives distinguish a present partial file from a
never-created copy, bind the object and original task root, and retain replaced
or reappeared material. They record actual physical removed bytes. Their pure
intent validator can inspect history; terminal units cannot perform a new
unlink. The separate `frontiercloud-python-target-private-cancelled` receipt and
version-2 progress journal separately prove the complete logical roster, actual
removed/absent copies and fresh retained Source authority. Logical quota refunds
use the admitted unit size exactly once; they must not masquerade as physical
deletion of bytes that were never copied. Master pending/copying rows must not be
rewritten into invented successful verification just to satisfy old receipts.

### Legacy Source encryption representation gate

The first complete Compose/Admin browser candidate correctly refuses an actual
encrypted upload during read-only planning. The existing Python Direct upload
stores `content_encrypted=1` in its completed native upload journal, but its
`media_encryption` descriptor is owned by the Master; the Source has no descriptor
row. The first `PlanStore.registration` candidate equated the Master descriptor digest with
the native Source descriptor digest and reports
`migration_source_registration_mismatch`. Content validation and Target
publication also assume those two representations have one digest. The earlier
service fixtures supplied descriptors at both stores and did not establish this
ordinary-upload integration boundary.

The repair binds the actual signed Source metadata/version separately from the
Master-owned descriptor. A missing Source descriptor requires an exact original
Master ciphertext checksum and size; unknown original checksums and different
nonempty descriptors remain blockers. No Source backfill, premaster movement or
backend decryption occurs. Planning remains read-only. New immutable objects add
the exact `master_encryption_sha256` field; `encryption_sha256` still describes
the actual Source. Unknown extra fields remain refused, and older objects retain
their original single-digest interpretation. Target publication/cancellation use
the Master digest. Source verification and removal still use the original native
digest. Current-catalog cancellation and readonly history verify both authorities.

The V13 trusted Chromium/Compose component passes 22 checks and retains 21 actual
GUI requests. It uploads encrypted content through the ordinary Admin uploader,
plans without changing files/ownership, migrates two complete parents from Direct
to Relay, preserves logical IDs/catalog/encryption records and exact ciphertext,
and decrypts the migrated object in the actual browser. Master has no transferred
payload files. Three independent Compose nodes retain identities, one CPU/512 MiB
Web limits and five low-CPU recovery samples. It stops at `cleanup_pending` with
all Source originals retained; it does not establish elapsed-hour terminal release.
Later unit/SQL candidates extend this repair to mixed cancellation and immutable
retirement history. Exact-final-source full discovery/native/fleet gates remain.

### Mixed-partial candidate and evidence scope

`resource_cancel_roster` accepts pending/copying metadata only after complete
two-peer enrollment, verifies each admitted binding and keeps full copy proofs
mandatory for publishing/committed units. Cancellation authority checks current
Source ownership independently from copy progress. Native private cancellation
first reconstructs the admitted parent digest in bounded metadata pages, then
persists an identity-pinned unlink or absence intent for each object. It records
processed logical counts separately from actual deleted counts/bytes and absent
copies. A missing journal cannot adopt even an empty pre-existing file. Complete
verified/shadow-publication receipts keep their existing strict version-1 rules.

The private path retains fresh Source signatures and its original task root,
joins native I/O, refuses readers/traffic conflicts and commits each intent
before unlink. Lost unlink acknowledgements replay only that durable intent;
reappeared or changed files retain exclusions and capacity. Native settlements
refund each uncommitted parent's logical reservation once. Master pending and
copying rows become cancelled without fabricated verified bytes or Target SHA.
Committed placement remains final and uses its original cleanup grace.

Terminal native history independently verifies private intents and sums their
actual physical counts, binds the distinct signed receipt and settlement chain,
and performs no removal or new signing. Recorded-receipt replay uses a read-only
connection: opening a nested SQLite write transaction under the caller's native
transaction was caught by the first complete mixed test and fixed without
relaxing identity or filesystem checks.

The V4 candidate passed four focused SQLite/Linux cases in 123.244 seconds and
four actual three-database MySQL cases in 74.0378 seconds. They exercise one
committed parent, a parent with a verified copy plus a partial encrypted copy
plus an unstarted object, and a wholly unstarted parent. They also cover actual
unlink before failed SQL acknowledgement, Target reconstruction/new runner,
reappeared files and an unjournalled empty file. Complete virtual-time cases
retain Source originals for cancelled parents, finish the committed parent's
delayed cleanup and verify all three read-only histories. Signed malformed
private receipts are rejected. These are not actual elapsed-hour proofs.

The trusted-TLS component profile uses independent Source/Target processes,
actual planning/enrollment/control endpoints, ciphertext byte ranges and a lost
received private receipt followed by an independent Target process restart. It
checks the same durable receipt after restart and deliberately stops before the
real one-hour deadline. Its output explicitly leaves whole-task completion,
Source-drain release, independent Master process restart and elapsed-hour
acceptance false. All Direct/Relay pair/database results retain their exact
inputs and source snapshots; full release acceptance is not inferred from this
bounded component profile.

The component matrix passed all four Direct/Relay pairs on both SQLite and
actual MySQL, with 24 checks per configuration and 2,327 recorded actual HTTP
inputs. SQLite runs took 80.5154–84.7125 seconds; MySQL runs took
86.1757–95.0644 seconds. Each parent and its two peer processes shared one CPU
and 512 MiB. The five quiescent samples per run were below 0.56% of one CPU;
no OOM or OOM kill occurred. Raw memory-pressure/reclaim counters are retained;
this is not a claim of zero memory pressure. Source served 4,201,104 payload
bytes per run while Master exchanged about 434 KiB of signed metadata, with
each recorded control input bounded to 16 KiB. No media key was opened.

The earlier full-copy/shadow paths passed 38 Target/unit-settlement regressions
in 507.919 seconds on V4. Thirteen read-only cancellation-history cases, seven
routing cases and thirteen ordinary successful-finalization cases passed in
627.637 seconds on V6. Their first attempt exposed an obsolete fixture that
assumed a fixed path was the first hash-ordered unit. The corrected fixture
assigns its committed/retained roles to actual pipeline order and continues to
create real files, plans and proofs; publication guards were not relaxed.

The private `partial-cancel-progress-evidence.json` collection contains exact
host scripts/inputs, source proofs and deltas, outputs, failed attempts and
independently checked MySQL network/container/volume cleanup. Its combined
archive SHA-256 is
`73a3e4990e3f057ca107c97e7302a9d526640b15155abeb4ed484cd629340b47`.
Application runtime files match across V3/V4/V5/V6; test fixtures/native drivers
retain their individual source scopes. Older native orchestration that attempts
to publish every parent before the first commit still needs adaptation to the
approved serial pipeline before the complete final native gate can pass.
Independent Master process recovery, actual-hour mixed-partial terminal release,
the latest complete discovery and public/Admin controls remain required. No
push, production, Gin or owner-preview update occurred.

### Private focused pipeline evidence

The `parent-pipeline-source-v10` snapshot passed 21 SQLite/Linux cases in
216.279 seconds with a one-CPU/512-MiB parent fixture: five enrollment/pipeline
cases, fourteen publication regressions and two scheduler cancellation cases.
The latter prove cancellation during incomplete metadata enrollment and after
all parents have committed. The all-committed case preserves cutover, waits for
the original cleanup grace and uses normal successful native receipts. Metadata
enrollment cancellation preserves originals and passes all three read-only
retirement-history verifiers without issuing a capability or changing a row.

Five actual three-database MySQL pipeline cases passed in 60.2951 seconds on
that snapshot. These include registration reply loss/replay, native byte and
ownership checks, a scheduler reconstruction between parents, per-parent
publication refusal and registration-capability separation. SQL observation
checks bound whole-roster recounts. The MySQL network, containers and recorded
anonymous volumes were independently checked absent after exact fixture cleanup.
These are service-client/native-file tests, not actual HTTPS or an independent
Master process restart. Cleanup time is virtual.

The earlier V3 source passed 56 journal/scheduler/routing/pipeline cases in
375.269 seconds. The separate V6 private-copy primitive snapshot passed eight
cases, including actual partial-byte accounting, known absence, replaced roots,
changed/reappeared files and refusal to unlink terminal units. That primitive
has not yet become a complete mixed-partial cancellation settlement. Earlier
20-case SQLite/seven-case MySQL whole-task routing records retain their old
scope; they do not prove the new mixed-partial path.

The archive retains three interrupted test attempts: an invalid multi-chunk
encryption fixture corrected to use actual per-chunk AES-GCM; a copy-fault test
corrected to reach copy readiness after metadata enrollment; and an obsolete
task-wide error assertion corrected to require the actual per-unit refusal.
Collector singular/plural parsing failures are retained separately and did not
cause business tests to be rerun. Exact host inputs, outputs, source proofs,
per-snapshot deltas and cleanup records are in the private
`parent-pipeline-progress-evidence.json` collection. Its combined archive SHA-256
is `3de13f3c106de785ae13b4f2c86fb40760b453a1080453c8c33d3843671b6f7a`.
The current complete unit discovery, trusted-TLS transport matrix, real cleanup
hour, mixed-partial terminal settlement and public/Admin integration remain
release gates. No push, production, Gin or owner-preview update occurred.

## Durable foundation

The generation-5 candidate adds independent migration tables and keeps previous
business tables, node identity, vault data and encryption facts intact. The
old compute-job tables are not reused. The schema signature records columns,
constraints and indexes in deterministic order for both database dialects;
separate-process hash seeds must produce the same signature.

A job records its Master, source and target identities and relationships, plan
hash, total object/byte counts, revision and committed bytes. Complete parent
units and object-level proofs are separate rows, allowing later inventory and
execution to page through large plans without retaining the whole inventory in
memory. Plans, scoped ownership fences, target capacity, source drains, storage
journals, peer bindings and reader leases have their own durable records.

`resource_migration_journal.Journal` performs short database transactions only.
It locks the current Master identity and task, uses a monotonically increasing
runner epoch and task revision, and rejects expired or superseded workers.
Claiming a job is exclusive even when a caller reuses the same runner ID. No
network operation or traffic wait belongs inside these database transactions.

Manual pauses persist their prior phase and cannot be resumed by the traffic
scheduler. An automatic pause can be converted to a manual pause. Automatic
resume reads the actual monitor snapshot instead of accepting an API/client
boolean. Control changes invalidate the old runner while retaining its epoch.
The separate generation-7 candidate extends the stored resume phase to
publication and delayed cleanup. Pause takes effect between bounded native
steps; a lost in-flight reply is reconciled from the same journal on resume.
It does not reverse a committed parent or remove Target publication. The older
generation-5/6 definitions and signatures remain unchanged. Post-publication
cancellation remains a separate implementation gate; the current control
refuses it before changing task state or releasing any resource.

Committed bytes come from complete committed parent units, not upload/copy
progress. Entering commit requires every planned object to have matching
source/target hashes and exact verified byte counts. Completion requires all
parent units cleaned and the final task-wide inventory/accounting/release gate.
Finalization implements that gate and requires both native terminal receipts;
these primitives must not expose completion based on unit states alone. A cancellation
request retains reservations and fences;
only storage reconciliation may subsequently release them. Journal state alone
does not prove physical publication or cleanup and grants no media permissions.

### Post-publication cancellation work in progress

The published Gin reference distinguishes committed ownership from an
uncommitted Target shadow. Python's existing early-abort path remains in place;
the public post-publication cancellation path still refuses the operation until
the complete native protocol is integrated and accepted.

| Authoritative Master state | Required cancellation treatment |
| --- | --- |
| Parent uncommitted, private Target copy | Verify retained Source authority and bytes; stop old native I/O, persist an exact unlink intent and remove only the owned private copy |
| Parent uncommitted, Target assembly or publication exists | Repeat Source verification, reconcile the assembly/rename journal, remove only the verified Target shadow and its exact native registration; Source originals remain |
| Parent committed, including a lost native acknowledgement | Never issue shadow-deletion permission; reconcile the same immutable commit and retain Target ownership; managed Source cleanup still needs its original grace and reader checks |
| Mixed task finishes cancellation | Retain committed-byte history and current owners, require native receipts for every cancelled/cleaned unit, refund only remaining owned capacity and release only this task's fences |

`resource_cancel_files` adds the bounded one-link filesystem primitives. They
perform no reads of payload content and do not confer cancellation authority.
Its caller must validate a fresh signed Master permission and Source proof,
join/fence existing native I/O, reconcile any two-link assembly first, and
persist the exact intent before invoking unlink. An unacknowledged rename is
located by the recorded assembly inode and parent/root identities. Reappeared
or changed files, extra hard links, symlinks, replaced roots/directories and
unknown nonempty directories refuse removal; there is no recursive deletion.

`resource_cancel_authority` adds short signed Master permissions after a locked
current-catalog, descriptor, scope, relationship, runner and reservation check.
The source-catalog validator is shared with ordinary cutover without performing
the placement write. A Master commit record prevents permission even when the
native Target still reports an uncommitted publication because its acknowledgement
was lost. This permission alone is not native Source verification and grants no
Source deletion, ownership rollback or quota release.

`resource_cancel_source` now retains the actual signed response from the
existing native Source-verification endpoint. The shared bounded control client
verifies that signature before optionally returning its complete envelope.
Cancellation checks the fresh Master permission, Source signer, task/unit
version, complete object/byte counts, epoch and expiration before storing a
forwardable proof. A Master signature cannot substitute for the Source
signature. No Source private key is delegated and no media bytes pass through
the Master. Source changes or unknown siblings block the proof; a lost response
or failed Master proof-audit transaction must be reverified on retry. Expired
permission/proof has a distinct code from invalid signature/shape.

The cancel-shadow-source-v3 snapshot passed 52 focused SQLite/runtime/native-file
cases in 356.794 seconds and 23 actual three-database MySQL authority, Source
signature and cutover-regression cases in 269.0798 seconds. The file primitive
subset is eighteen cases; Master permission has ten cases and Source signatures
have seven. Tests include opaque GCM bytes, recordings, lost rename replies,
file reappearance, unknown material, descriptor closure, current owner/crypto/
relationship changes, a committed Master with missing native acknowledgement,
Master-resigned Source payload rejection, lost Source replies and proof-audit
rollback. These use signed in-process service clients and real native files;
they are not real TLS/fleet/CPU profiles or completed cancellation acceptance.
All clients were limited to 1 CPU / 512 MiB. Exact inputs, source hashes and
prior snapshots are retained privately in `cancel-shadow-progress-evidence.json`.
Native Target rollback and mixed-task terminal orchestration are tracked below.
The public post-publication cancel restriction is retained
until those gates pass; no unit permission alone releases data, quota or fences.

### Native Target rollback candidate

`resource_cancel_target` adds `cancel-stop` and `cancel-target` controls on the
existing signed, authenticated transfer API. A short cancel grant fences older
copy/publication epochs even while traffic is high. Physical work still needs
the actual low-traffic and clear-reader observations, native writer exclusion,
an exact Master uncommitted-owner authorization and the real Source signature.
Committed native units and any retained commit record refuse rollback.

The Target persists an exact per-file unlink intent before removal, validates
current registry/encryption facts and preserves encryption tombstones. One
object is removed per step. Known two-link assembly is reconciled first; a lost
rename is located through the saved directory identity. The known empty ready
directory from a failed initial preparation has its own persisted rmdir intent.
Unknown entries, changed files/metadata, replaced roots, unsafe links and new
readers retain the operation and reservations. Started I/O is joined before a
canceled request can release its native writer lock.

Final metadata reconciliation is paged at 16 objects. The Target issues a signed
unit receipt only after its exact roster, metadata and removed-directory checks
agree. It includes the final Source signature and a hash of the corresponding
Master authorization, avoiding repeated long paths in the bounded control reply.
Retries verify the stored receipt, Source proof and current private task root;
they do not remove later external material. The Target protocol changes no global
owner, Source original, quota reservation, drain or scope. Mixed-task Master/
Source terminal settlement, integration with the existing partial-copy abort,
complete fleet profiles and complete cancellation acceptance remain gates. This
candidate handles complete verified units, including interrupted assembly/
publication, and refuses an incomplete copy roster. In that historical snapshot,
ordinary publication required every planned copy to be verified. The approved
per-parent pipeline above replaces that task-wide publication barrier, while the
partial-copy cancellation receipt/settlement extension remains unfinished. The native unit receipt does not
authorize a task-wide refund or expose public mixed cancellation.

The cancel-target-source-v5 snapshot passed 63 focused SQLite/Linux cases in
442.850 seconds and eighteen actual three-database MySQL Target cases in
223.5926 seconds. Coverage includes plain/ciphertext files, three opaque recording
footer profiles, retained Source and Master rows, readers, stale or forged Source
proofs, commit refusal, interrupted hard-link assembly, lost rename/unlink replies,
failed audit transactions, changed registry state and unknown external material.
The actual ASGI/HMAC test is distinct from verified HTTPS acceptance.

The V6 native profile then passed sixty checks on SQLite and sixty on actual
MySQL in 161.3804 and 167.3984 seconds respectively. Both used verified HTTPS,
actual direct Source bytes and independently restarted Target processes. A real
Range read creates a durable lease; cancellation waits for actual clock expiry,
refuses new reads after its persisted intent, and observes the unchanged
thirty-second quiet interval after restart. Source signatures cross the actual
TLS control path. A discarded deletion reply is reconciled after process restart;
a terminal receipt replays during startup quiet without deleting later external
material. Source bytes, identities, global owners, quota and fences remain intact.
Each client group including its child had 1 CPU / 512 MiB; final five whole-cgroup
CPU samples were 0.231–0.281% for SQLite and 0.236–0.274% for MySQL. The MySQL
server had its separate 1 CPU / 768 MiB limit. These are complete fixture costs,
not the marginal overhead of cancellation. There are 288 and 284 recorded actual
Master control requests respectively. No production, Gin or owner preview update
is involved; the public post-publication cancellation gate remains closed.

Terminal replay was then hardened on V8: the complete typed progress, final
Master/Source proof, receipt counters and receipt roster must remain present and
consistent. Missing or malformed progress cannot fall back to weaker receipt
validation or an unclassified exception. Ten damaged-journal variants preserve
later external material and refuse replay; restoring the original journal returns
the same signed receipt. All nineteen Target cases pass on SQLite in 233.092
seconds and actual three-database MySQL in 232.6719 seconds. The repeated native
TLS profile passes sixty checks per engine in 160.9824 and 166.0214 seconds, with
290 and 283 recorded Master requests. Final whole-cgroup CPU samples are
0.205–0.260% and 0.233–0.251% respectively under the same limits. V5/V6 results
above remain their original snapshots. Exact source deltas, failures, native
inputs and fixture cleanup records are in `cancel-target-progress-evidence.json`.

### Native Source retention candidate

`resource_cancel_source_peers` adds the authenticated `cancel-source-stop` and
`cancel-source-retain` controls. Stop fences old copying grants even at peak
traffic, waits for the actual Source I/O lock and migration read leases, closes
only this task's metadata scans, and persists its stopped state. Ordinary
playback leases do not block retention because it removes no Source bytes.

Retention requires the current short Master authorization and the Target's
signed shadow-removal receipt. It checks the original path/root binding, native
unit, Source drain and scopes, then reuses the existing bounded Source verifier:
eight object identities and thirty-two directory entries per step. It does not
rehash media payloads, read media keys or import business data. Traffic spikes,
authorization expiry and superseded epochs stop the scan. Started metadata I/O
is joined before the Source lock is released, including request cancellation.

The signed retained-unit receipt binds the unit/version/counts, both peer
identities, plan, Target receipt, Master authorization, fresh Source verification
and original roots. The Source stores the receipt and retained unit atomically
with its audit. Committed native units refuse retention. Unknown files, changed
objects/roots, damaged records and failed audit transactions retain the original
data, reservations and exclusions. Replay validates the complete stored proof and
current root binding; it never deletes later external material. These are unit
facts, not permission to refund capacity, release scopes/drains or finish a task.

The cancel-retain-source-v2 snapshot passed 51 focused Source/Target/verifier
cases in 610.486 seconds and 35 cutover/scheduler regressions in 383.348 seconds.
Its sixteen Source cases separately passed on three actual MySQL databases in
210.7537 seconds. These cover opaque recording footers, ordinary versus migration
readers, old-copy fencing at peak, live writer exclusion, bounded-scan traffic
interruption, joined cancellation, failed audits, changed originals/roots,
unknown siblings, damaged receipts, new-epoch replay and actual ASGI/HMAC nonces.
Those unit clocks and ASGI clients are distinct from actual TLS acceptance.

The V3 native profile passed seventy-five checks per database in 227.0203 seconds
on SQLite and 233.5332 seconds on MySQL, recording 387 and 386 actual Master
control requests. It restarts the independent Source process three times with
the same identity/keys/database, repeats the actual thirty-second quiet window,
recovers an interrupted metadata scan, and replays its signed receipt before
startup quiet completes. Source and Target processes remain live, old HMAC
nonces still refuse, and source bytes, business/crypto rows, quota and fences
remain unchanged. No complete-task cancellation or elapsed cleanup-hour claim
is made by this profile.

All three fixture Python processes share 1 CPU / 512 MiB. Both profiles record
zero OOM/kill events and five final whole-cgroup CPU samples below 0.5% of one
core. The group reaches its memory limit with reclaim pressure; the SQLite peak
briefly exceeds it by one 4 KiB page. Native peer RSS is approximately 190–192 MiB
per process, includes fixture imports and shared pages, and is not additive.
These are whole-fixture measurements, not cancellation's marginal memory cost.
The MySQL server has its separate 1 CPU / 768 MiB limit. Exact source deltas,
inputs, resource counters and cleanup are retained in
`cancel-retain-progress-evidence.json`; no production or owner preview is changed.

Remaining integration must connect the per-unit settlement candidate below,
retain the source drain through committed-unit cleanup and its original grace,
and require fresh native task settlement before completion.
The published Gin control does not pause its `cancelling` phase; Python does not
need a new schema generation merely to add that unsupported pause. Existing
manual pauses still require manual resume. All old schema definitions remain
unchanged, and the public post-publication cancellation restriction remains.

### Per-parent cancellation settlement candidate

`resource_cancel_unit_peers`, `resource_cancel_unit_receipts` and
`resource_cancel_unit_master` add a separate settlement after the Target shadow
receipt and Source retention receipt. This candidate remains behind the public
post-publication cancellation restriction.

The Source performs another bounded metadata verification of the original
parent, retaining its earlier signed proof. It then releases only that parent's
native scope and signs its settlement. The Target requires this Source
settlement and checks that the old shadow remains removed before releasing its
own scope and refunding exactly this unit's reservation. Neither step deletes
Source files, changes placement, releases the Source drain or finishes a task.
Each receipt, scope change, refund and audit commits in one local transaction.

The Master checks both native settlements, current Source ownership, object
roster, its own scopes and exact reserved balance. Its atomic transaction marks
the unit/objects cancelled, refunds only this unit's Master reservation and
releases the two Master scopes. Unrelated scopes and reservations remain.
Signed reservation status reports `cancelled_bytes` separately from
`committed_bytes`; already settled cancellations must not be counted as still
reserved after a restart.

A null-input authenticated settlement query observes pending work or returns
the stored signed receipt; it does not authorize new settlement. Lost replies
recover through these records without repeating a refund. A live Master runner
recovers the committed revision after a lost final reply, while an expired or
superseded runner gains no authority. Historical replay verifies the recorded
proof and root binding without removing later files under a released parent.

This is a unit protocol. Whole-task reconciliation, mixed committed-unit
cleanup, terminal receipts, public cancellation and scheduler integration still
require their separate acceptance. No new schema generation or implicit
ownership rollback is introduced.

The cancel-unit-source-v6 snapshot passes nineteen focused SQLite/native cases
in 296.610 seconds and the same nineteen cases against three actual MySQL
databases in 245.7411 seconds. They include opaque recording footers, null or
damaged journals, fresh Source changes, foreign Target material, independent
reservations, failed native/Master audits, lost replies, expired runners,
signed-field mutations and actual ASGI/HMAC replay checks. The separate V3
snapshot passes 109 verifier, cancellation, cutover, scheduler, reservation and
ordinary-reader regressions in 1053.404 seconds; it predates the V6 null-journal
guards. Their source proofs remain separate.

V6 verified-TLS profiles pass 108 checks per database in 292.0642 seconds on
SQLite and 297.1327 seconds on MySQL. They settle two parents, deliberately lose
both native settlement replies, restart the Source and Target independently,
recover the stored receipts and preserve later files without another refund.
The profiles retain 501 and 488 actual Master control inputs. Ordinary CA and
hostname verification remain enabled. They keep the Source drain and whole task
active; they do not assert public or complete-task cancellation.

The three fixture Python processes share 1 CPU / 512 MiB and record five final
CPU samples below 0.5% of one core, with zero OOM/kill events. Both groups reach
the memory limit with reclaim; MySQL's group peak briefly exceeds it by two
4 KiB pages. These are whole-fixture counters, not marginal runtime memory
measurements. The MySQL server has a separate 1 CPU / 768 MiB limit. Ten formal
schema inputs remain unchanged. Exact inputs, source deltas, failed earlier
fixtures, resource counters and cleanup are retained in the private
`cancel-unit-progress-evidence.json` set. Production and owner preview remain
untouched.

### Master cancellation coordinator candidate

`resource_cancel_master` and `resource_cancel_client` now drive both native
stops, fresh Source proofs, Target shadow removal, Source retention and the
per-unit settlement above. Stop grants and signed native replies are persisted
in the task. Restart does not repeatedly close the active metadata scan; a new
runner epoch validates the old records before obtaining its own stops. The
complete copied roster is checked before the first persisted stop record.
Subsequent pages verify its immutable reservation binding and the current
parent rather than rescanning every task object. Damaged or empty stop records
refuse work.

Each native observation has strict typed fields and receipt checks. A passive
Source-retention query returns pending work or its durable signed receipt; it
does not authorize removal, a refund or task completion. The coordinator stores
Target removal before requesting Source retention and resumes lost replies
without asking the operator to supply proofs. Current progress is retained per
parent. Already committed parents receive their missing native acknowledgements;
their ownership and original one-hour cleanup grace remain final.

The cancel-master-source-v4 snapshot passes eighteen focused cases in 296.412
seconds and the same eighteen cases on three actual MySQL databases in 243.4172
seconds. These cover a mixed committed/cancelled task, native and coordinator
restarts, six lost stop/removal/retention/settlement responses, partial assembly
and registry publication, opaque recording footers, audit rollback, partial
copy refusal, changed originals, malformed signed observations, actual
ASGI/HMAC replay and absence of repeated whole-task object scans. The separate
V3 snapshot passes 52 earlier unit-settlement, cutover and Source-retention
regressions in 677.874 seconds, before the V4 scan optimization.

V4 verified-TLS profiles pass 65 checks per database in 231.1612 seconds on
SQLite and 236.7685 seconds on MySQL. They run the coordinator against independent
native processes, restart the Source three times and the Target twice, retain
a live Target Range reader until its real lease expires, block new readers
after cancellation intent, repeat actual quiet windows and recover discarded
terminal peer replies. They retain 451 and 439 actual Master requests. This
profile is separate from the earlier per-unit profile, with its own source
proof. CA and hostname checks remain enabled; it is not a public-network,
browser or Nginx acceptance claim.

The three fixture Python processes share 1 CPU / 512 MiB, record five final CPU
samples below 0.6% of one core and zero OOM/kill events. Reclaim occurs at the
memory limit; MySQL's group peak briefly exceeds it by two 4 KiB pages. These
are whole-fixture counters. The MySQL server has a separate 1 CPU / 768 MiB
limit. Exact inputs and proofs are retained in the private
`cancel-master-progress-evidence.json` set. Its separate full-source discovery
later completed 1,686 cases with four fixture errors and seventeen skips in
3,840.829 seconds. The generation-9 repair evidence below does not turn that
earlier failed sweep into current full-suite acceptance.

The public cancellation restriction remains. This coordinator ends at settled
uncommitted parents and acknowledged committed parents. The mixed cleanup
candidate below follows that boundary. Fresh whole-task native settlement,
source-drain release, terminal task state and scheduler/Admin integration still
require their own implementation and acceptance. No production or owner preview
update occurs.

### Mixed committed and cancelled cleanup candidate

`resource_cleanup_master` now accepts a cancelling task only after every parent
has its native commit acknowledgement or cancellation settlement. The selected
cleanup parent must still be committed. Source additionally requires its durable
cancellation fence before including settled cancelled parents in the complete
roster. The original signed commit, one-hour grace, fresh Target proof, live HTTP
and durable-reader checks, metadata census and journaled unlink remain required.
Cancelled parents never become cleanup candidates.

The cancel-mixed-source-v3 snapshot passes thirteen SQLite cases in 260.784
seconds and the same thirteen cases against three actual MySQL databases in
184.7239 seconds. The same runtime passes 46 existing Source-cleanup, cutover and
finalization cases in 627.917 seconds, plus all four source policies. Cases cover
mixed plaintext/ciphertext preservation, incomplete
settlement refusal, the missing native cancellation fence, unchanged grace,
Target damage, active readers, traffic deferral, lost replies, unlink followed
by audit rollback and new-runner recovery. These cases use virtual time. Separate
verified-TLS elapsed-hour profiles on SQLite V3 and MySQL V4 now pass 57 checks
each. Actual Source sockets outlive their original capabilities and block unlink
until closed. The original hour is unchanged; cancelled originals, committed
Target bytes and the Source drain remain. Their 1,283 actual HTTP inputs and
exact generation-8 snapshots are retained in `cancel-mixed-hour-progress-evidence.json`.
These are not current generation-9 census or whole-task cancellation acceptance.

Per-parent cleanup does not reconcile Source's cached usage, release its drain
or complete the task. Cancelled originals and any external files remain at
Source. Whole-task cancellation needs a fresh, fenced current-resource census;
the successful-migration empty-Source finalizer cannot be reused for retained
resources. Exact overlays,
eight completed host input records, failures and prior fixture removal checks
are retained in `cancel-mixed-focused-progress-evidence.json`. The separate hour
profiles are excluded from that earlier frozen archive. This is uncommitted development
evidence, not release acceptance.

### Current retained-Source census candidate

Generation 9 adds only `migration_cancel_census`, an indexed native-resource
metadata journal. It leaves generations 2–8 signatures and business tables
unchanged. The current Master's authenticated control installs a durable Source
write fence after native units are cleaned or cancellation-settled. Roster pages
observe current native paths, stable IDs, descriptors and file stats; filesystem
pages reconcile every regular file against those indexed snapshots. Renamed
parents are observed at their current paths. Unknown files, changed snapshots,
missing registered media, links and damaged control state refuse proof and keep
the fence. File I/O does not hold the native identity transaction. Inactive
walkers close after five minutes; restart repeats the census under its retained
fence. No media payload or recording footer is read or decrypted.

The typed Master client verifies bounded signed replies and preserves the
original proof expiry across a lost reply. This observation does not establish
Master catalog agreement, change cached usage, release the Source drain or
complete cancellation. Those steps, scheduler/Admin integration and final
transport/resource acceptance remain unfinished. Current native and database
results retain their own exact snapshots and input records.

The V6 snapshot passes 23 focused SQLite cases in 493.925 seconds and 33 actual
MySQL cases in 369.0832 seconds. The latter includes generation-8 to 9 recovery,
short/long/Unicode database lock names, exclusion and release. MySQL names within
the existing lock-name limit retain their legacy identity; longer names use a
stable digest for both acquisition and release. The existing generation-2 to 8
checksums match the earlier snapshot exactly.

Verified-TLS V6 profiles pass 76 checks per engine: the earlier coordinator
profile plus current Source census, a further real Source restart, lost proof
reply, original proof expiry, preserved unknown files and a retained ordinary
Source Range read. Live census requests reject media-payload reads. The profiles
retain 584 SQLite and 570 MySQL HTTP inputs. Three Python processes share one
CPU and 512 MiB; five final CPU samples are below 0.6%, with no OOM/kill event.
Memory reclaim occurs at the limit and group peaks exceed it transiently by one
or two 4 KiB pages; summed process RSS is not group memory. MySQL's server has
its separate one-CPU/768-MiB limit.

The V7 fixture repair passes 34 cases in 6.783 seconds with one explicit MySQL
skip. It repairs the transaction-order double's new-table reflection as well as
the four older full-suite fixture errors. The failed 109-case V6 intermediate
run and failed 1,686-case generation-8 full sweep remain retained. None is
reported as a passing current full suite. Exact sources, 1,154 HTTP inputs,
18 completed host input records and fixture removal checks are in
`cancel-census-progress-evidence.json`.

### Current Master agreement candidate

`resource_cancel_catalog.CurrentCatalog` installs a durable task-plan write
fence before observing Source. It projects current Master ownership in pages
of at most sixteen objects, preserving stable IDs, current renamed paths and
encryption descriptors. Its ordered catalog digest and current counts must
match the native Source's signed census. Missing recordings, mismatched paths,
orphan encryption and extra Source-owned catalog entries cannot establish
agreement. Metadata queries release the Master transaction before network I/O.
The fence covers the Source's current media and recordings while leaving reads
and unrelated media paths available. Restart keeps the fence and a new runner
recounts current metadata.

This step uses existing task metadata, adds no database generation, changes no
usage or quota, and releases neither native reservation nor Source drain. It is
not yet invoked by the public cancellation path or scheduler. Whole-task native
settlement, exact current-usage reconciliation, durable terminal receipts and
Master cancellation completion remain required before that integration. The
initial SQLite candidate passes 27 focused/guard/rename cases in 249.588 seconds.
The V2 agreement snapshot passes ten SQLite cases in 253.539 seconds, 43 actual
MySQL cases in 548.2813 seconds and 81 verified-HTTPS checks per engine (312.8221
seconds SQLite; 324.6565 seconds MySQL). Its 1,229 actual HTTP inputs include
lost signed Source replies, re-created Master coordinator objects and retained
write fences. These inputs and exact source hashes are retained in
`cancel-catalog-progress-evidence.json`. A wrapper's JSONL postprocessing failure
and the separate corrected parsing record are preserved; business tests were
not rerun or their exit codes rewritten for that repair.

The later V4 fix distinguishes normal retired encryption rows (SQL NULL
descriptor, retained file ID) from live orphan descriptors. Both ordinary
encrypted deletion after unit settlement and committed encrypted Source cleanup
must permit agreement without discarding anti-reuse history. A live orphan
still refuses. Six SQLite cases pass in 141.023 seconds, and four actual MySQL
cases pass in 53.9741 seconds. This focused follow-up is retained in
`cancel-catalog-supplement-v4.json`; it does not relabel the preceding V2 TLS
profiles as testing the later fix. Complete current-source acceptance remains
required before release.

### Retained Source release candidate

`CurrentCatalog.authorize` signs a short-lived agreement only after verifying
current Master ownership, the native Source census and committed-byte
accounting. It binds the task, both identities/relationships, runner, original
counts, committed and retained subsets, current catalog/root hashes and the
exact Source proof. A fresh grant cannot extend the proof's original deadline.
Signing alone changes no quota, ownership, native reservation or drain.

The authenticated internal `cancel-source-release` control requires that
agreement and the matching live native physical census. In one audit-backed
transaction it reconciles only native Source usage to current retained bytes,
marks that reservation released and removes only its own migration scopes,
capacity, drain and migration read capabilities. It reads no media payload and
deletes no media. Ordinary playback leases, administrator drains, node identity,
Source bytes and Master state remain. Missing physical state, foreign work,
bad signatures or expired authority fail closed. Audit failure or expiry during
the transaction rolls the complete release back.

The durable Source reply carries its original agreement, census and receipt.
After a lost reply, a new runner can query that receipt using a fresh authorized
grant. Historical proof validation uses its completion time; replay does not
reset later usage or repeat the release audit. Target settlement and atomic
Master completion are still required: the Master task/drain remains active,
and public post-publication cancellation is not enabled by this primitive.

The initial authority snapshot passes twelve protocol/SQLite cases in 137.882
seconds. The initial Source release passes ten SQLite cases in 225.637 seconds;
the later reply includes the original witnesses for restart recovery. Sixteen
actual MySQL authority/release cases pass in 260.0691 seconds with exact fixture
cleanup. The V3 verified-HTTPS profiles pass 91 checks per database: SQLite in
312.7214 seconds and MySQL in 318.3521 seconds. They include a further real
Source process restart after a committed release reply is discarded, recovery
of the original witnesses/receipt, exactly one release audit and an ordinary
retained-media Range read. Their 1,269 actual HTTP inputs and ten completed
host input records are retained in `cancel-source-release-progress-evidence.json`.
Both profiles enforce one CPU/512 MiB across the three Python processes and
record no OOM/kill event; final CPU samples remain below 0.3% of one core.
The MySQL server has its separate one-CPU/768-MiB limit. These are Source-side
release observations, not Target release, Master completion, public cancellation
or full transport/resource acceptance. The later V2/V3 runtime inputs match;
their changed native scripts and profile wrappers are listed in that evidence.

### Target and Master terminal release candidate

`resource_cancel_target_release.TargetRelease` revalidates every cancelled
settlement and every committed Target directory. It does not read or decrypt
media payloads, change ownership or reset Target usage. Signed durable cursor
checkpoints prevent an altered SQL cursor from skipping a unit. A separately
signed preparation record is committed before known empty task staging is
removed, so an audit/SQL failure after directory removal can resume after
restart. Unknown private entries refuse release and remain untouched. Final
release removes only this task's scopes, zero capacity entry and migration
leases; other reservations, playback and public bytes survive.

Eight focused SQLite cases pass on the initial snapshot in 208.604 seconds.
Eight actual three-database MySQL cases pass on the later portable-root-proof
snapshot in 133.734 seconds, with exact container/volume/network cleanup.
Real TLS acceptance initially exposed an incorrect test assumption that every
cancelled task contains a committed unit. The corrected profile subsequently
observed the required fresh quiet window after a real Target restart; its
helper now waits instead of assuming immediate readiness. Both failed inputs
are retained. The complete later terminal profiles pass as described below.
Mixed committed/cancelled coverage
in these focused cases uses declared virtual cleanup time, not an elapsed hour.

`resource_cancel_finalize_master.CancellationFinalization` queries durable
Source release before attempting a new current census. It binds the original
Source witness to the frozen Master catalog, then obtains Target release and
rechecks bounded pages of cancelled settlements or committed cleanup history.
Only the final transaction reconciles retained Source usage, expires its runner
and releases this task's Master scopes/capacity/drain. A signed terminal record
makes a duplicate completion read-only, preserving later writes and usage.
The initial seven SQLite cases pass in 186.224 seconds. The later eight-case
snapshot adds final-lease expiry rollback and passes SQLite in 212.044 seconds
and actual MySQL in 137.2244 seconds, with exact fixture cleanup.

The subsequent trusted-TLS terminal profiles pass 107 checks per database:
SQLite in 344.958 seconds and MySQL in 362.795 seconds. They complete the
all-cancelled Source/Target/Master chain, including two further independent
Target process restarts, durable preparation, a discarded committed Target
release reply, a fresh observed quiet interval and terminal Master replay after
an injected later usage change. Master reconstruction uses a fresh coordinator
object, not an independent Master process; that distinction is recorded.
Five final cgroup CPU samples remain below 0.5% of one core, with no OOM event.
All three Python processes share one CPU/512 MiB; MySQL has one CPU/768 MiB.
The final native runner now includes this profile for both databases. Its
nineteen runner/CI contracts and four source policies pass after wiring. The
1,384 recorded control HTTP inputs, fifteen completed host input records,
original failed profiles, exact-source bridges and fixture cleanup checks are
retained in `cancel-terminal-progress-evidence.json`. The focused, native and
wiring snapshots have identical application runtime inputs; their test/script
and documentation differences are enumerated. These observations do not turn
the older 1,775-case source sweep into acceptance for the later application.
Neither coordinator is wired into public cancellation or the scheduler yet.
Partial-copy mixed cancellation, already-released success histories, retirement
history acceptance and the full transport/resource matrix remain release gates.

### Read-only terminal cancellation history candidate

`resource_cancel_terminal_receipts` now supplies shared immutable signature
validation for replay and retirement. Whole-task completion records a distinct
cancelled plan phase. Master history still validates the immutable preview,
complete original roster, each committed cleanup or cancelled settlement, and
the signed final census. Mixed committed bytes are retained and checked against
the sum of cleaned units; a terminal state string alone grants no exception.

`node_retirement_cancel_history` adds an explicit native released-cancellation
branch. It requires all admitted object bindings, rejects an unexplained Source
cleanup journal for a retained unit, checks Target unlink intents and their
signed roster, verifies original/fresh retained-Source evidence, and repeats the
Target preparation census. Source/Target release signatures must match their
exact reservation, roots and unit counts. Existing successful/aborted history
checks remain separate. Verification does not issue grants, request media,
read payloads, remove files, reset usage or release anything.

The V4 thirteen-case snapshot passes SQLite in 359.157 seconds and actual
three-database MySQL in 202.1462 seconds, with exact fixture cleanup. The earlier
V3 snapshot passes twenty-one history/completion cases in 566.100 seconds and
seventeen existing successful/aborted histories and independent-retirement
regressions in 242.274 seconds. V4 additionally strengthens native root,
progress and Source-settlement cross-checks; that exact runtime is used by V5.
An initial history
fixture mixed real observation time with virtual cleanup time; its original
failure is retained. The fixture now installs one clock before admission,
without relaxing product timestamp checks. A follow-up fixture hook was initially
installed before native reservations existed; its failure and correction are
also retained.

V5 passes 110 trusted-TLS checks per database: SQLite in 345.4357 seconds and
MySQL in 356.757 seconds. Master, Source and Target history verification leaves
every migration table unchanged and issues no capability, after the real native
Source/Target restarts and terminal replay. Master recovery remains coordinator
reconstruction, not a separate Master-process restart. Five final CPU samples
are below 0.6% of the shared one-core quota, with no OOM/kill event. These profiles
retain 1,381 actual control HTTP inputs and ten completed host input records in
`cancel-retirement-history-progress-evidence.json`, together with exact-source
bridges, failed attempts, logs, archive hashes and cleanup checks. The V4/V5
application runtime matches; only the added native history assertions differ.
The complete exact-source, partial-copy/mixed elapsed-hour and full transport
matrix remain pending. No public or scheduler post-publication cancellation
integration is claimed by these private coordinator/history checks.

## Read-only native inventory candidate

The current candidate now has authenticated inventory control routes under
`/internal/v1/migration/inventory`: `start` accepts only `request_id` and
`scope`, `page` accepts only `session_id` and `sequence`, and `close` accepts
only `session_id`. Scope is one of the node's fixed `data`, `media` and
`recordings` roots; callers cannot supply host paths. Only the Follower's active
upstream Master relationship may use the routes. Existing certificate-verified
HTTPS, raw-body HMAC and durable request-nonce checks apply. Responses are
signed with the existing node identity and are never publicly cacheable.

One native session per node has a fixed five-minute deadline and a process
incarnation. Matching start retries retain that deadline. Page retries can
reuse the last result without advancing the walker. Each page carries at most
64 entries and a 48 KiB payload budget, cumulative entry/byte counts and a
rolling SHA-256 transcript bound to the signed header. A joined reaper closes
expired/abandoned sessions and shuts down with the application.

The walker opens directory components without following symlinks, keeps at
most 64 directory frames, and observes metadata without opening file contents.
It reports symlinks, nonregular files and hard-link counts rather than following
or hiding them. Unsupported paths, changed directories/roots and excessive
depth invalidate the observation. Missing roots are reported without being
created. Free space is an observation, with `writable_verified: false`, not a
write probe or a capacity reservation. Overlapping-root capacity must later use
a conservative minimum, never a sum of apparent free space.

High business traffic returns a waiting response without advancing the walker
or extending the deadline. No SQL transaction or native mutation lease is held
while waiting. Bounded filesystem operations join before their descriptors are
released, including request/helper cancellation and cancellation while opening
a new walker. Closing and expiration never remove files.

The Master inventory client verifies node signatures, fresh request/session
bindings, incarnation, scopes, page order, strict entry shapes, cumulative
counts and the rolling transcript before passing each bounded page to its
observation consumer. The future planner must persist paths with cross-page
uniqueness constraints and reconcile them against the full catalog. Inventory
transport completion alone is not a complete, authorized migration plan.

## Source-content observation candidate

The candidate also supplies `/internal/v1/migration/observe/start`, `status`
and `close`. Start accepts exactly `request_id`, `resource_kind`, `object_id`
and `user_id`. Registered media IDs resolve through the native `media_objects`
and encryption tables. Recording IDs are confined to the authenticated current
Master's relationship/user namespace. There is no caller-supplied filesystem
path, URL, hash, capacity or encryption key.

One expiring, joined observation worker runs per node. Matching retries reuse
the same job and deadline; a competing request cannot add another hashing
worker. It waits for low traffic before opening a source, then hashes with
64 KiB reads and rechecks the active relationship between chunks. Waiting
retains no SQL transaction or native mutation lease. File identity, metadata,
size, link count and the currently registered object are checked before and
after hashing; changes invalidate completion. Encryption tombstones, wrong
ciphertext lengths, unregistered media, symlinks and hard links fail closed.

Recording trailers are bounded by the existing 2 MiB metadata contract. Legacy
recordings without trailers, valid plain trailers and opaque encrypted lyric
snapshots are preserved. A present but invalid/oversized trailer is rejected
rather than interpreted as valid legacy metadata. Neither media nor recording
lyric ciphertext is decrypted. The node does not open its media premaster or
derive media keys for an observation.

Queued/running/failed/cancelled states contain progress and a bounded error code,
not a usable content result. Only completed jobs return the exact length,
SHA-256, native identity, metadata/encryption digests and immutable source
version. Replies bind the current Master, relationship, source node, selector,
request, process incarnation and five-minute deadline with the existing node
signature. Credentials/identity changes also invalidate cached results.
The Master client verifies these bindings, expiry, progress, source-version
digest and result shape. Closing/expiry/shutdown join the actual read before
releasing its file descriptor. No source file is renamed, overwritten or
deleted by this API.

The native HTTPS acceptance now covers registered plaintext media, real GCM
ciphertext and a recording with an encrypted lyric footer. It uses the actual
HTTPS control client and refuses wrong CA/hostname verification. A guard that
fails on media-premaster access or media-key derivation remains active during
these calls. This proves source observation, not migration or authority cutover.

## Read-only plan candidate

`resource_migration_planning.Planner` binds the Master catalog and whole-parent
units to native registrations, actual filesystem inventory and completed content
proofs. It rejects MasterLocal, stale/offline relationships, unsupported native
objects, split/nested ownership, missing or extra sources, occupied target parents,
pending operations and insufficient quota or physical capacity. A target conflict
includes every descendant of the complete selected parent, even nested directories.
Recordings bind the source relationship/user parent to the target relationship/user
parent without modifying their Master business rows. Titles/lyric snapshots are
represented by hashes in the plan spool; media metadata is never decrypted.

The one planner worker observes real traffic and has a fixed thirty-minute budget.
Catalog objects and hashed path markers spool through short SQL transactions in
batches of sixteen. Registry replies are bounded, identity-signed and tied to the
current observer/inventory process incarnations. The source roster catches orphan
registrations as well as physical inventory mismatches. Ordinary filesystem
conflicts are counted across the complete inventory, with at most sixty-four
hashed-path samples. A malformed/duplicate transcript stops observation; duplicate
markers persist independently of a classification transaction's rollback.

After hashing, a second complete source/target scan verifies source stat identity,
detects missing/rewritten sources and newly introduced target files, and obtains
fresh native free space. A new registry and Master-catalog check rejects changed
metadata, relationships, quota or logical ownership. Capacity takes the minimum
free space across observed target roots, subtracts the physical reserve and
current logical reservations, and never adds together roots on the same disk.
No readonly probe claims that an actual publication has been write-tested.

Ready previews expire after five minutes and bind the current planner incarnation.
Retries of a live request reuse its observed result. Status includes readable
source/target paths and progress, with at most sixteen objects and 48 KiB per page.
`inventory_complete` describes a completed native scan; `blockers_complete` is true
only after content, final inventory and catalog/relationship rechecks finish.
An early blocked/cancelled scan does not claim to have discovered every possible
problem. Expired previews are reaped in bounded batches, preserving admitted plans,
task-referenced plans, audits and every resource file.

Eighteen focused cases use three actual SQLite stores, native files and signed
service clients, covering plaintext/ciphertext, recording parent mappings,
many conflicts, post-hash changes, latest capacity, process restart, cancellation,
duplicate rollback, response size and observation cleanup. This in-process
transport is not a native HTTPS fleet acceptance claim. The candidate planner is
not connected to Admin start/admission and creates no task, scope, drain, capacity
reservation or physical mutation.

A subsequent isolated native check uses the actual HTTPS transport, durable nonce
authentication, node signatures and two peer ASGI endpoints against separate stores.
It waits for real startup quiet, accepts a plain/GCM complete-parent preview and
then refuses a new target conflict. Wrong trust roots and hostnames are rejected;
source bytes remain unchanged. This is a single CPU-limited test process, not
separate-host transfer or full migration execution acceptance. It is included in
the native runner and sanitized evidence selection.

## Native preparation and execution

### Native reservation candidate

The unpublished generation-5 candidate now has a fourteenth journal table,
`migration_peer_reservations`. Earlier private generation-5/6 fixture receipts
remain tied to their original source; their databases are not silently reopened
or relabelled under the extended signature. Published Python generation 4 remains
the data-preservation baseline.

The native `/internal/v1/migration/reservations` controls bind the current active
Master, native identity, relationship, source/target roles, Master-provided plan hash,
whole-parent transcript and exact object/byte totals. Existing verified HTTPS,
HMAC/raw-body nonce admission and node signatures apply. Every signed reply also
binds a fresh caller request ID, the immutable preparation digest and current
revision. No credential or media key appears in the reply.

Begin serializes with native mutations and the identity row. It refuses pending
writers/deletion recovery and other unresolved migration authority. Source owns
a durable upload drain; Target reserves actual logical capacity after checking
the conservative minimum of native free space and remaining quota. No free-space
sum or write probe is claimed. Ordinary media and recording receives subtract
existing reservations from their physical-space admission as well as quota.

Parent scopes append in deterministic unit-ID order with a durable chained
transcript and totals. Matching retries are idempotent; changed, nested, occupied
by a pending upload, out-of-order or inconsistent units are refused. Recording
parents explicitly map the source relationship/user to the target relationship/
same user. Seal verifies stored unit/scope counts as well as the transcript.
Status/retry checks the actual native capacity and source drain, so a missing
reservation cannot be replaced by a positive report from old counters alone.

Preparation and its scopes/quota survive restart and age. An unstarted abort
requires no object transfer journal, capability binding or advanced unit state,
then atomically removes only its own metadata and subtracts its own reservation.
Audit failure rolls all changes back. It never unlinks, renames or rewrites a
resource. Published/started work requires a different verified reconciliation.

Both `writable_verified` and `execution_authorized` remain false. These native
controls supply durable preparation, not a Master admission, transfer grant,
physical write test, completed copy or cutover. Their exact-source focused,
native TLS/process and actual MySQL results are tracked in the current parity
ledger; full regression and current repeat results are recorded separately.

### Master preparation candidate

The coordinator admits a ready preview into a durable `admitting` task, under
the Master mutation fence and node-row transaction. It revalidates approval,
expiry, observation epoch and member facts before holding exact target capacity
and source/target drains. Whole-parent scopes then install in short bounded
transactions. While any scope list is incomplete, ordinary path mutations are
refused rather than assuming that an absent scope is writable. Reads remain
available. This conservative gate applies on both Master and native peers.

Each unit version hashes the ordered source content proofs and logical IDs.
The same ordered transcript drives native begin/unit/seal, including recording
relationship-parent mappings. A renewable SQL runner epoch fences stale Master
coordinators. Restart reuses the admitted preview and immutable task ID instead
of requiring a new volatile planner epoch. Matching native retries do not add
capacity twice. No SQL transaction or Master mutation lock spans peer HTTP.

The final round verifies current peer identities, actual native reservation
authority, unchanged registration/content versions, complete native inventories,
free space and final Master catalog/member facts. Registry permission excludes
only the exact signed preparation binding; unrelated quotas, writers, journals
or reservations continue to block. Repeated admission scans reuse one bounded
marker namespace. `prepared` is a separate non-executable state; the transfer
journal cannot claim it. Both write-probe and execution flags remain false.
Staging writability and a verified direct Source/Target path still need their
own admission steps before a later runner may queue the task.

Unstarted cancellation is durable and explicit. Master waits for both native
terminal confirmations before subtracting its own reservation or releasing its
scopes/drains. A native request which never arrived gets a bound aborted header
without a disk probe or file changes; delayed begin then returns that terminal
state. If acknowledgement, audit or capacity validation fails, surviving fences
remain and the same task can be reconciled. Any transfer binding, storage/object
journal or advanced unit state refuses this preparation-only cancellation path.

A stale/read-only plan cannot be treated as execution authorization.

### Direct-copy candidate

The Master execution coordinator repeats preparation observations and requires
an actual private staging write/fsync/read probe plus a signed Source/Target
HTTPS exchange before queueing a task. Its entry points are
`TransferMaster.preflight`, `start` and bounded `copy_one`; they are internal
domain controls, not a completed Admin module or operator release.

Master-signed grants bind both node IDs/public keys/endpoints/relationships,
preparation/plan transcripts, runner epoch, object/parent versions, content hash
and a forty-five-second window. Each Source range also requires a Target-signed
request binding that grant, exact range and a durable single-use nonce. A Target
never receives the Master's relationship HMAC credential. Nonces and expired
reader leases reap in bounded owned batches; no resource file is removed by age.

Target pulls at most 4 MiB per step using 64 KiB reads/writes. A durable journal
and immutable object binding precede private nofollow staging. The probed task
directory's device/inode and restrictive ownership are retained across process
restart. Length comes from the actual file, and an incremental hash context is
reused across ordinary steps. Restart rehashes retained bytes in bounded steps;
large prefixes do not require a full in-memory buffer or repeated normal scans.
Fsync, complete length and the expected original digest precede verification.

Master only records verification metadata and keeps committed bytes at zero.
Target creates no ordinary media registration or public path. Source plaintext
and ciphertext, Master logical IDs, crypto descriptors and business references
remain unchanged. Ordinary transfer-window expiry is not cancellation or
permission to release reservation/scopes or delete unknown staging resources.
Master and peer traffic checks stop further steps; complete fleet scheduling,
publication and source cleanup still need their own gates.

### Active cancellation candidate

`Cancellation.request` persists intent and invalidates the old Master runner.
`advance` signs a higher-epoch stop for both peers and waits for actual local
writer locks and migration reader leases. Ordinary playback reader leases are
separate and retained. Every native data grant then fails against the durable
aborting state; a short window expiry cannot restart copying or release quota.

Target removes at most one registered staging object per step. The journal
records its device/inode identity before the first write. Cleanup verifies that
identity under the original private task/base-directory identities, persists a
full-stat delete intent, unlinks without following links, fsyncs, and only then
records absence. Retry after lost acknowledgement accepts verified absence;
replaced files, symlinks, new contents after a delete intent and unknown directory
entries are retained. Only empty known directories can be removed. Partial probe
markers have their own fixed task/binding content and root proof.

Target settlement waits for every owned staging entry and directory to be
reconciled. Source releases its metadata fences only afterward, without opening
or deleting an original. Both native terminal acknowledgements precede Master
settlement. Each transaction subtracts only its own capacity and preserves other
reservations; audit failure rolls back local settlement. Published/committing
material refuses this cancellation path and requires publication reconciliation.

Native streaming cleanup shields its short database close from disconnect
cancellation before releasing the reader lock. This prevents a closed descriptor
from leaving an unreleased connection or live migration read lease. Actual
joined writes must complete before cancellation may unlink their temporary file.

### Bounded error details

Migration application errors return a fixed code in a node-signed envelope bound
to the exact raw request hash, path, node, Master, relationship and HTTP status.
Clients expose that code only after verifying every binding. Unknown/unsigned
upstream bodies remain generic status failures. Encoded control responses are
refused before decompression. Source data clients use the same verified errors
and distinguish TLS verification, connect/unavailable and timeout categories.
No arbitrary upstream body, private credential or filesystem exception text is
forwarded as an operator error.

Only source-to-target data connections may transfer migration bytes, including
Relay placements. Master supplies bounded authenticated control, never a media
relay. Peer capabilities must bind identities, relationships, object/version,
task, purpose and deadlines, use ordinary verified TLS, and reject replay or
changed bindings. Failure to connect directly must stop the operation.

### Target directory publication candidate

`resource_publish_master` drives bounded metadata and publication calls. The
Target first verifies the complete Source-bound object roster, even when counts
and total bytes match. Each descriptor must match its original copy capability;
no premaster or decryption key is opened. A verified file's exact native stat
binds the private staging inode. Hard-link/unlink assembles an owned private
directory without another payload copy; an atomic directory rename publishes
one immediate parent. Native media IDs and encryption descriptors are then
registered through durable per-object SQL phases. Master business rows are never
imported. Empty existing directories are removed only by identity-checked rmdir;
changed/nonempty destinations, unknown files and changed roots preserve material
and reservations. This mechanism coordinates managed writers, not arbitrary
external filesystem writers.

Lost link/unlink, directory-move and SQL/audit acknowledgements reconcile the
same inodes. Restart resumes registration. Renewed publication receipts require
another bounded physical/registry check and expire with the current signed
capability; they are not fresh signatures on a historical file observation.

The actual verified-TLS candidate gate passed 37 checks in 102.2583 seconds,
including a partial transfer failure, two independent Target process restarts
(the second after directory rename), plaintext and GCM ciphertext, original
inode equality and preserved native encryption metadata. The whole parent/child
cgroup had one CPU and 512 MiB, peaked at 404,361,216 bytes, and its last five CPU
samples were 0.480, 0.442, 0.521, 0.430 and 0.573 percent. Source originals totalled
9,498,019 bytes; Master control bodies totalled 150,319 bytes. Body observations
are not NIC measurements. This is not the separate-host Direct/Relay fleet gate.

Five actual MySQL cases passed in 75.6482 seconds with three separate node
databases per case. They exercise mixed plaintext/ciphertext registration,
link/unlink recovery, directory-move acknowledgement loss, a new receiver and a
lost Master audit acknowledgement. The preceding 42-case SQLite run passed all
12 publication cases and the copy/cancellation cases except an old protocol
assertion that still classified the new `publish` purpose as invalid. The
corrected assertion also requires a well-formed publish capability to be refused
by the Source byte endpoint; its focused repeat is recorded separately.

Target publication alone keeps every Source original, global owner and quota
reservation unchanged. Split/nested ownership remains refused by planning. The
later cutover section supplies the Master transaction and native acknowledgements;
task-level reconciliation and delayed cleanup still gate complete migration. Original
source resources remain until verified cutover, a cleanup grace interval and
reader-lease checks.

### Fresh Source unit verification candidate

After Target publication, Master requests a new short-lived Source proof. The
Source checks eight immutable copy bindings per step, reconstructs the admitted
unit roster hash, and opens each current native object to verify its complete
version, metadata and encryption digest. A directory iterator checks at most 32
entries per step against indexed bindings; an unregistered direct child, changed
directory, changed inode/bytes/ctime or changed native metadata refuses a proof.
Nested directories are separate ownership boundaries and are not counted as
this unit's direct files. Task-wide native inventory and the planner's prohibition
of split/nested catalog ownership are still required; this is not a recursive
filesystem deletion proof.

No media payload is hashed again or decrypted. The previously verified content
hash is accepted only while the exact Source version is unchanged and durable
managed-write exclusions remain held. A receiver restart loses its in-memory
cursor and restarts observation. A completed Source proof is stored with the
root/directory identities, current expiry and unit digest. Master must reject an
expired or malformed signed response and still leaves ownership and reservations
unchanged. The private journal schema remains unpublished; new indexed binding
columns are part of generation 5, not an edit to an already released migration.

The next exact candidate TLS/process gate passed 40 checks in 102.5641 seconds,
including Source membership and metadata verification. Its one-CPU/512-MiB cgroup
peaked at 404,254,720 bytes and ended at 0.534, 0.488, 0.461, 0.530 and 0.584 percent
CPU. Master control bodies totalled 162,261 bytes. This snapshot precedes the
additional stricter Master response checks; their focused evidence is separate.

Eight actual MySQL publication/Source-verification cases passed in 99.8649 seconds
on that runtime snapshot. The subsequent focused snapshot passed 28 SQLite
cases in 210.497 seconds plus CPU, source/deployment, environment, English-comment,
diff and shell-syntax checks. Those cases additionally reject expired signed
responses, a boolean progress count and a contradictory published phase, and
retain cancellation/admission refusal for unreconciled material. Native and
MySQL results retain their own earlier exact source proof; the later runtime
delta is confined to the stricter Master response checks. They are not relabelled
as a complete native or release run of the final candidate.

### Master cutover and native acknowledgement candidate

`resource_cutover_master.Cutover` requires unexpired, current-epoch Target
publication and Source verification receipts. Under the existing exclusive media
fence and node-identity transaction, it verifies all unit object hashes/counts,
current catalog ownership/path/size/ETag and raw encryption facts, complete-parent
membership, both scopes, the Source drain and exact remaining owned capacity.
Media encryption comparison is independent of a legacy MySQL text collation.
The transaction updates only catalog placement, exact Master used/reserved bytes,
the complete-parent commit record and task progress/revision. Current lyrics,
titles, filenames, visibility, play statistics, creation/update facts and logical
IDs are never restored from a historical plan. Source and Target native object
IDs and encryption descriptors remain unchanged.

Execution validates current immutable node/relationship/configuration bindings.
It does not demand the admission-time usage snapshot after a legitimate cutover
or unrelated Target upload. Native reservations and the Master commit transaction
independently verify current owned capacity. Admission itself retains its stronger
original frozen-preview comparison. Initial acknowledgement tests exposed this
distinction: the old preparation-facts check incorrectly required the original
full reservation after Master had consumed it. That failure is retained with the
subsequent corrected evidence.

`resource_commit_peers.CommitPeers` validates the complete immutable commit and
its native receipt. Target moves exactly one unit from reserved to used in the
same SQL transaction that marks its native unit committed. Source acknowledges
the retained original and drain; its cached physical replica charge is not
presented as a deletion. Remaining native capacity derives from committed unit
journals. A repeated request, lost Native/Master audit acknowledgement or new
runner cannot charge Target twice. Source refuses old copy capabilities once its
final unit proof exists. Acknowledgements hold the peer I/O lock and do not wait
for the heavy-work low-traffic window. Every scope and Source original remains;
the one-hour cleanup deadline is recorded, not treated as proof of cleanup.

The candidate's actual verified-TLS gate passed 54 checks in 104.0535 seconds.
It transfers 9,498,019 original bytes, commits both immediate parents, discards
one received acknowledgement before Master journaling, restarts the actual Target
process again and reconciles without a second charge. The real native media
handler serves matching `bytes=3-9` plaintext and ciphertext with the original
global logical IDs after cutover. Target starts independently three times across
the partial-copy, publication and acknowledgement stages. These endpoint apps
and child share one CPU/512-MiB cgroup; peak memory was 405,286,912 bytes and final
CPU samples were 0.467, 0.545, 0.498, 0.432 and 0.456 percent. Master control bodies
totalled 178,135 bytes. The deliberate controller-side acknowledgement discard
is not claimed as a network cable disconnect. The reader is a test HTTP client,
not browser audio/video decoding or the full Direct/Relay fleet matrix.

On the same runtime, 23 focused SQLite/API/copy/cancellation cases passed in
173.297 seconds, plus source policies. Six real MySQL cases passed in 60.7549
seconds. Recording coverage publishes one Source relationship/user directory
into the Target namespace with legacy, plain-footer and opaque GCM-lyric-footer
files. It compares all raw bytes, retains current Master title/lyrics/filename
and confirms no recording business rows appear at either storage node. These
are byte/metadata proofs, not browser recording decryption or listening evidence.

Catalog reads add an indexed durable migration namespace from positive task
committed-byte totals. Task history is retained and those totals never rewind.
The namespace changes in the ownership SQL transaction even when Redis is
unavailable or a runner exits before notification. Failed transactions preserve
the warm Source cache; native acknowledgements and cleanup bookkeeping do not
change this namespace. A failed SQL counter read bypasses cached ownership.
Four SQLite cache cases passed in 36.373 seconds on a later snapshot, with source
policies. Eight actual MySQL cases on that snapshot passed in 98.2986 seconds,
repeating the six cutover/recording/recovery cases and adding committed namespace
and warm-cache acknowledgement checks. The earlier TLS receipts keep their own
source proof; cache and schema-index deltas are not relabelled as that native run.
Unknown leftovers must be reported rather than deleted to bypass admission.

## Ordinary reads and fresh Target verification candidate

Authenticated media GET/HEAD and recording stream/download requests create
ordinary playback leases in the native node database before returning a storage
redirect. OPTIONS and rejected capabilities create none. Each lease covers the
signed capability deadline plus five seconds, survives ASGI response completion
and contains resource identities and a filesystem version, never media bytes or
decryption keys. Expired-row reclamation is bounded to thirty-two rows per read;
live migration leases and other members' leases remain. The media fence and node
row serialize new leases against a future cleanup intent. An active Source unit
in cleanup state rejects new reads; released historical tasks do not block a
later ownership round trip. Files and catalog entries are rechecked after
authorization, including expiry during filesystem I/O.

Capability leases alone cannot describe a download lasting past its original
capability. Native ASGI delivery tracks the complete response lifetime. The
Nginx local counter protocol is now version 2 and reports active business
requests as well as actual bytes. Bounded per-worker shared slots preserve old
workers' open requests during graceful reload. Only confirmed dead workers or
their normal exit clear those slots; uncertain state, invalid counters and stale
observations refuse reader clearance. The new shared layout requires the normal
Nginx container restart; an incompatible old shared zone cannot be reused by
reload. No product Docker control or filesystem payload inspection is added.

The actual Nginx fixture transferred matching 2 MiB sendfile bytes and a 2 MiB
upload. A held 1 KiB/s response remained counted across graceful reload. Killing
that old worker reconciled its count to zero. The real quiet recovery took
32.090 seconds. These are one-worker HTTP/Unix-socket fixture results, not a TLS
fleet or isolated observer-overhead measurement. Thirty focused SQLite/API/
monitor cases passed in 23.096 seconds. Seventeen actual MySQL cases passed in
185.3204 seconds, repeating eight cutover/cache cases and nine ordinary-reader
cases. The updated verified-TLS cutover fixture passed 54 checks in 103.9212
seconds with both post-cutover plaintext/ciphertext reads and all Source bytes
retained. Each run retains its own source proof.

`resource_target_verify` supplies bounded post-commit Target re-observation.
It requires the native committed record and retained scope, checks the complete
admitted roster, current published file versions, native registry, encryption
descriptors and directory membership, then signs a short-lived Target proof
bound to the immutable Master commit and current runner epoch. A restart starts
the scan again. Master requires both native accounting acknowledgements and
verifies both response and nested Target signature. There is no media rehash,
decryption, ownership change or Source deletion. Eight initial SQLite cases
passed in 77.870 seconds, including changed bytes with restored mtime, registry/
descriptor changes, unknown files, restart, failed audit and invalid replies.
Two later SQLite cases passed in 20.103 seconds: the recording relationship
namespace with opaque footers and a lost Master audit after Native success.
The updated actual verified-TLS fixture passed 59 checks in 134.3722 seconds.
It obtains authenticated outbound heartbeats from both live peers, observes the
real post-restart quiet interval, verifies fresh Target proofs for both units
and checks persisted playback leases after actual plaintext/ciphertext Range
responses. There is no certificate-verification bypass. The fixture transferred
9,498,019 committed bytes, observed 265,895 Master control-body bytes and retained
all Source originals. Its combined one-CPU/512-MiB fixture peaked at 408,989,696
bytes; final CPU samples were 0.547, 0.452, 0.526, 0.517 and 0.576 percent.
These remain combined app/controller/child measurements, not per-node overhead.
Ten actual MySQL Target-verification cases passed in 125.5826 seconds, repeating
all ten SQLite scenarios on the later runtime. Actual inputs, failed builder
invocation, sanitized logs and separate source proofs are collected privately
in `readers-target-progress-evidence.json`. The later per-unit cleanup candidate
below builds on those checks; task-wide final inventory and independent
retirement remain separate gates.

## Delayed Source cleanup candidate

`resource_cleanup_master` requires all complete-parent commits and both native
accounting acknowledgements, exact retained reservation/scope state, current
Target catalog ownership and the original `committed_at + 3600` deadline. The
unpublished generation-5 unit schema records a typed native-acknowledgement flag;
it changes in the same transaction as the second Master acknowledgement. A
missing or expired Target proof starts fresh Target verification, not deletion.
The Source receives only a signed narrow grant and the nested Target proof.

`resource_source_cleanup` independently verifies its native commit, grace,
relationship, scope, Source drain and original Source receipt. Its persistent
cleanup intent blocks new Source read capabilities. A bounded fresh roster
checks every original file version, registry/encryption metadata and actual
directory membership. Root/directory identity and permissions remain bound;
ctime changes alone do not authorize cleanup or make a safely resolved unknown
entry impossible to reconcile. The complete current roster must pass again.

An active business response or live durable read lease prevents removal.
Unavailable or stale counters are never interpreted as no readers. Each unlink
has its own committed intent before nofollow identity-checked I/O and directory
fsync. The Source media fence and I/O lock remain held, while the database
node-row lock is released during filesystem completion so status/authentication
can continue. Native registry removal, encryption tombstoning, exact journal
progress and audit then commit together. A crash or expired grant after unlink
requires complete fresh reconciliation before the known absence is settled.
A missing file without its own valid intent, a replaced root, a changed file or
an unknown entry is never silently removed or acknowledged as success.

This removes only the planned Source replicas. It never deletes Master business
rows, Target bytes, node keys, certificate data, containers, host directories or
volumes. Recording files keep their opaque footers and Target relationship/user
namespace. Native cached usage remains conservative during per-unit cleanup;
only the eventual complete Source inventory may reconcile that cache. Every
scope, drain and native reservation remains until task-wide reconciliation.
Per-unit replies explicitly report `native_usage_reconciled=false`, and Master
progress reports `task_completed=false` even after every planned file is gone.

Task-wide completion is a separate signed protocol. The Source binds both
storage roots during its authenticated preflight. After every unit is cleaned,
it pages those original roots without reading file contents. Unknown files,
root replacement, native registry rows, active readers, pending writers or
foreign transactions prevent completion. Only a complete empty census permits
reconciling its native used counter to zero and releasing this task's own
capacity, scopes and migration drain. Empty original directories are retained.

The Target requires the durable Source terminal receipt, then repeats bounded
registry, encryption, file-version and complete-parent membership checks. It
removes only known empty private staging directories. Unknown staging entries
are retained and block release. Other Target usage and reservations are preserved.
Both nodes retain signed terminal receipts and migration history. Replaying a
released receipt cannot clear later counters, remove new files or release another
task's exclusions. The Master separately checks current ownership and native
receipts before atomically releasing its own exclusions and completing the task.
The generic journal checkpoint cannot substitute for these final censuses.

Eleven SQLite finalization cases passed in 155.027 seconds; the same eleven real
MySQL cases passed in 149.6184 seconds. They cover completion/history, unrelated
Target quota, read-only terminal replay, unknown files, replaced roots, live
readers, foreign reservations, Source and Master audit rollback, lost replies,
restart/new-runner recovery, changed Target files and invalid receipt types.
These use the declared virtual grace clock and are not elapsed-hour acceptance.

The development fixtures use declared virtual clocks to test grace boundaries,
crash/expiry recovery and SQL transactions. They do not prove a genuine elapsed
hour. The separate V3 verified-TLS gate passed 76 checks, including 3,609.3147
seconds in the cleanup phase, a real Source socket held beyond grant expiry,
cleanup refusal until that socket closed, retained Target originals and both
native terminal receipts before completion. Source snapshot/proof and CPU/memory
results are retained in `finalization-progress-evidence.json`; subsequent runtime
changes are not relabelled as that result. The mandatory extended gate is
`scripts/test-python-migration-hour.sh`: it keeps an actual Source HTTP response
open across the unchanged grace and capability expiry, with actual authenticated
heartbeats, before testing cleanup and terminal receipt release. It must run on
the final reviewed source with its own elapsed-hour budget, beyond short CI.
Complete native task inventory/fence release,
large/small-file resources, the full Direct/Relay matrix and independent
retirement remain required before release.

Sixteen current SQLite cleanup cases passed in 193.713 seconds. Sixteen actual
MySQL cases on the same runtime passed in 188.4912 seconds, using three separate
node databases in an isolated one-CPU/512-MiB test client. They cover original
grace refusal, plaintext/ciphertext and recording/footer preservation, active
HTTP observations and durable read leases, changed files and roots, unknown
entries, an explicit fixture-owner preservation/retry, unlink followed by SQL
or Master audit failure, restart/new-runner fencing, expired grants and invalid
progress types. A deliberately held unlink permits a concurrent transaction to
acquire the native identity row before the filesystem operation is released.
That is a transaction-availability proof, not a measured HTTP/CPU performance
profile. The earlier combined fifteen-cleanup/fourteen-journal snapshot passed
29 cases in 189.895 seconds. An initial supplemental test selector was misspelled;
the whole invocation failed and was retained, and the corrected case passed.
Exact inputs and separate source overlays are in
`source-cleanup-progress-evidence.json`. The later finalization V5 source snapshot
passed complete discovery: 1,121 cases in 1,544.642 seconds with eight optional
MySQL cases skipped. Those eight then ran against an actual disposable MySQL:
seven schema/DDL/retry cases in 21.340 seconds and the historical migration smoke
in 0.339 seconds. The smoke binds only its connection setting to that guarded
database; it still executes real SQL and the ordinary migration code. The V5
supplement also passed three real MySQL finalization cases in 31.4974 seconds,
covering stale cached Source usage, unknown private staging and an originally
missing recording root. There are thirteen distinct finalization cases across
the SQLite discovery and MySQL runs. Failed earlier invocations and the corrected
certificate-renderer harness are retained in `finalization-progress-evidence.json`.
None of these virtual-clock results is native elapsed-hour acceptance.

Existing ordinary upload, rename, delete, recording and node-configuration paths
now check durable write scopes/drains before changing resources or reserving
capacity. Both SQLite writer serialization and the MySQL singleton-node row lock
order admission against these writes. Identical heartbeat configuration preserves
reservations and drains; readable health/metadata remain usable. Master visibility,
priorities and playback facts remain independent business metadata. Stale scopes,
failed tasks and unresolved physical journals are not discarded by age.

Ten actual SQLite guard cases cover all-selected refusal, global parent scopes,
independent nested parents, native reconcile/rename preserving files, recordings,
reserved quota and identity/configuration changes. An actual MySQL fixture passed
six checks, including a concurrent writer holding the node row until its durable
reservation commits, and a later writer observing a committed scope. These are
transaction guard proofs, not implementation or acceptance of migration admission.

`resource_migration_scheduler.Scheduler` now drives preparation, direct copy,
publication, cutover, native acknowledgements, delayed cleanup and finalization
one bounded control step at a time. It runs independently of heartbeat and
Backup, checks the actual traffic monitor and retains durable manual pauses.
Network loss backs off without releasing task resources; integrity errors stop
until an explicit revision-checked retry. A grace wait does not monopolize the
serial runner, and shutdown joins the active step before releasing its runner
incarnation. Independent Admin migration controls are implemented as the candidate
described below. Retirement
preflight/confirmation/history controls now have their separate implementation
and evidence in `python-node-retirement.md`.

The earlier generation-6 scheduler-source-v3 snapshot passed 71 focused
SQLite/runtime cases in 101.948 seconds and 11 actual three-database MySQL
scheduler cases in 125.802 seconds. These include native plain/ciphertext bytes,
virtual cleanup grace, network recovery, manual/traffic controls, live foreign
runners, explicit integrity retry, shutdown and a real heartbeat transaction
while an outgoing transfer control is blocked. They are in-process signed
service-client checks, not TLS, elapsed-hour, full fleet or CPU-profile proof.
The separate generation-7 pause-source-v5 snapshot passed 79 focused SQLite/
runtime cases in 266.882 seconds and nine actual MySQL database cases. Its
seventeen three-database MySQL scheduler cases passed in 204.0704 seconds and use
virtual cleanup grace. They now also cover publication and due-cleanup manual/
traffic pauses, restart, a pause during a lost native publication reply and
light native commit acknowledgements before heavy-work traffic pause. The
quiet-window fixture observes the low-traffic transition before measuring the
unchanged thirty-second interval; a silent jump of its clock is not evidence
that the monitor observed that interval.

The formal original generation-6 schema fixtures preserve typed prior rows,
sealed node/S3 credentials, encryption descriptors and unrelated media hashes.
They are not execution of an old released application. SQLite faults after
DROP and RENAME roll back the complete swap; unknown task indexes, triggers,
temporary tables and incoming foreign keys refuse the upgrade without removing
them. Actual MySQL interruption after its single implicit-commit ALTER retains
the old marker; retry validates the changed check before advancing it. Exact
generation-4/5/6 signature comparisons passed. Source proof, exact inputs,
earlier failed assertions/fixture corrections and successful receipts are in
the private `pause-progress-evidence.json` set. These are focused candidate
results; exact-final-source full discovery, native TLS/fleet/CPU gates and the
published generation-4 upgrade/recovery remain required.

## Evidence limits

### Independent Admin candidate

The `Resource migration` module follows Nodes in the canonical Admin order.
It uses native HTML/CSS/JavaScript and `/media/admin/migrations`: bounded member
and history pages, read-only plan creation/status, confirmed plan admission,
task/unit/current-file progress, explicit pause/resume/cancel/retry. It never
accepts media bytes, node credentials, native grants or a filesystem command.
The existing Admin session/CSRF and secure-transport dependencies apply.

Application startup owns one planner and serial scheduler; joined shutdown
stops them before the shared node transport and database. Admission is queued
durably before HTTP success, keeping source/target drains and exact capacity.
The background scheduler performs the existing physical/protocol checks. Plan
approval binds its digest and both node identities. Repeated admission cannot
reserve twice. Every task action uses its current revision; a failed operation
does not imply released resources. An active admission lease refuses immediate
cancellation; after that bounded operation ends, an unstarted cancellation is
persisted and only native reconciliation can refund capacity.

Read-only projections exclude plan secrets, root paths, credentials and grants.
They distinguish current received/hashed bytes from verified copies and final
committed bytes. Unit pages are bounded by count and encoded response size;
member/history cursors retain complete enumeration. Manual pause survives a
new controller. A collapsed or hidden module stops periodic browser requests.
Expired preview authority requires a new plan, not a client-side override.

Eight real SQL/API cases pass independently on SQLite and actual three-store
MySQL. Seven module-order and eight transport/audit checks also pass in the
SQLite candidate. The browser component passes desktop/mobile layout, escaped
paths, file progress, plan approval, manual pause/resume, cancellation and paged
units retained through polling. Its API responses are explicit fixtures, not
evidence of actual GUI-to-peer execution. Complete authenticated Chromium,
native transport, fleet, hour and exact-reviewed-source gates remain required.

The separate independent-Master TLS component passes 25 checks in 82.583 seconds,
retaining the same identity and durable private receipt across epoch 5 to 6.
It does not finish the whole task or release the Source drain. The first actual-hour
extensions on SQLite/MySQL retain an original media capability and independent
Source socket under backpressure through the unchanged hour. Both then time out
while awaiting the expected reader-wait transition and fail acceptance. The
controller constructed `migration_waiting_waiting_reader`, outside its retryable
vocabulary. The corrected mapping has nine base/peer wait-state assertions.
A virtual-reader regression also exposed reuse of an unexpired Target proof from
an earlier runner epoch: recovery now authenticates that old proof and obtains a
fresh current-epoch proof, without reusing its cleanup authority. The focused
reader-completion case passes. Actual-hour reruns retain detailed boundary
diagnostics without altering deadlines or guards. No success is claimed until
the unchanged one-hour deadline, active-reader refusal, controlled close, final
native release and all three readonly histories actually pass.

The V17-source reruns now pass those boundaries on both SQLite and actual
three-store MySQL: 44 checks each, 2,837 actual HTTP inputs, 3,715.528/3,724.684
seconds total elapsed time. The Source HTTP reader survives the original
capability expiry and full cleanup hour. Canonical reader waiting prevents
deletion until controlled close and the quiet interval. Independent Master
takeovers preserve identity and durable receipts, then complete the task, release
its controls and verify all three readonly histories. Uncommitted originals and
current Master encryption/ownership facts remain intact. The entire controller/
peer group uses one CPU/512 MiB. OOM counters are zero; memory-limit and socket
pressure counters are not zero and remain in the raw evidence. Only the DR pair
is claimed for this actual-hour extension; complete final-source configurations
and release gates still apply.

The later complete GUI/Nginx Relay-reader profile waits the actual original
hour but its first run fails a fixture assertion. The serial scheduler waits
on the earliest parent while the test watches only the parent supplying the
held reader. Read-only SQL and original-byte observations retain that cause;
the later peer-connect error occurs after failed-fixture teardown. The next
profile observes every parent and verifies the real held socket after all
capability leases expire. Its terminal completion and independent GUI
retirement were pending at that snapshot; the first failure is not converted
into a success. V2 subsequently passes all 38 checks, waiting the original hour
and 3,679.161 seconds in its terminal phase. At controlled close the live reader
has consumed 2,576,384 bytes without EOF/error and no durable playback lease is
still live. Source cleanup then finishes all three parents, releases the task,
and actual GUI preflight, identity confirmation, signed receipt download and
independent retirement succeed. Its 1,252 GUI inputs include status polling;
the retirement workflow also retains its separate input records. Three capped
Web fixtures pass CPU recovery. This is a recorded SQLite GUI/Compose component,
not the complete final-source database/transport/fleet gate.

The later immutable-shape refinement adds the extended object only for plans
that recorded the explicit Source binding. Earlier plans keep their exact
original object fields across partial-copy recovery and a new controller.
An absent explicit binding cannot reinterpret a different Master descriptor as
an old Source descriptor. Seven focused SQLite and eight actual MySQL cases cover
that compatibility boundary and mixed cancellation/history with virtual grace.

SQLite journal tests cover concurrent claims, restart takeover, stale leases,
manual/automatic pause separation, progress assertions, failed-audit rollback,
generation-4 upgrade preservation, interrupted DDL and missing-index refusal.
The disposable real MySQL fixture covers implicit DDL commits, missing-index
repair on retry, preservation of prior table rows and node secret readability,
plus claim/control fencing. Complete native transfer, encryption, playback,
MySQL/SQLite fleet, one-hour source cleanup and independent retirement cases
remain required before release.
