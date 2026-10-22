# Python Compose operator: delivery and recovery contract

This is the implementation contract for the approved Python operator entry.
It is not a claim that the entry or its acceptance is already complete. Python
and Gin remain independent projects. The published Gin reference contributes
behavioral requirements, not a Go binary, database converter or image receipt.
Gin's unpublished complete-deployment-package upgrade is outside this scope.
Named acceptance snapshots retain their original versions and limits. Pending
or unexposed behavior in an older snapshot is historical; the current command
section and `python-current-parity.md` track the remaining release work.

## Implemented candidate and current limit

The one-shot host entry is `scripts/frontiercloud_operator.py`. It currently
exposes read-only `inspect` and `candidate`, `install` for new or interrupted
installations, `upgrade` for new or interrupted upgrades, plus `resume` and
`recover` for the same recorded upgrade transaction.
From the deployment directory, the host can run
`python3 /absolute/candidate/scripts/frontiercloud_operator.py inspect` as root.
An explicit `--project-directory` selects that same directory when invoking it
elsewhere. `candidate` only contacts the public Python repository and needs no
Docker control or root access. These two commands do not write deployment state.
`resume` requires the local root operator and a private operation index. It
continues the recorded forward upgrade or an explicitly selected controlled
recovery, without choosing recovery implicitly. It refuses unpublished acceptance
source rather than promoting a local fixture. `recover` selects the safe
old-deployment recovery for that indexed transaction; it takes no version, plan,
node-confirmation or backup-path parameters. Both commands default to the current
deployment directory and require the local root operator.

`upgrade` takes no version or recovery-file parameters. For a new operation it
selects the exact published main/CI proof and stages the verified Git tree.
The version-1 protocol in `protocol/python-operator.json` identifies the host
entry. The bootstrap executes that staged entry with isolated Python imports
and bytecode disabled. A private inherited file lock survives `exec`, so the
source change cannot open a concurrent-entry window. Requests, source receipts
and actual argument arrays stay under `/var/lib/frontiercloud-operator`, private
and outside all container mounts. Deployment configuration and source files are
not overwritten. The staged implementation prepares images and starts the
existing durable upgrade transaction.

An interrupted `upgrade` uses its retained exact source and operation reference
without querying a newer release. A matching ready release returns
`already_current` without downloading, building or restarting; that result does
not claim a fresh business-media read. A release without this host protocol
returns `source_operator_missing` before building or fencing. A target release
must provide this entry; local fixture success does not bypass
that public-release boundary. `resume` and `recover` also execute their retained
verified source, including after completion; neither selects a newer release.
The private command, intent and indexed source must agree before any operation.
Completed resume can return its recorded result; recovery never becomes a
forward upgrade. Exact-final-source, public-release and fixed-role fleet gates
remain required for this renewal.

### Installation and shortest commands

The commands below require a release declaring the operator protocols.
Run its verified host entry as root from an independent deployment directory:

| Operation | Command |
| --- | --- |
| Install business SQLite/MySQL | `python3 scripts/frontiercloud_operator.py install` |
| First install of Direct/Relay Storage | `python3 scripts/frontiercloud_operator.py install --storage` |
| Retry either installation | `python3 scripts/frontiercloud_operator.py install` |
| Select the newest published update | `python3 scripts/frontiercloud_operator.py upgrade` |

Business engine selection remains in `.env`; Storage requires its documented
hostname/endpoint and transport configuration. These are normal configuration
inputs. The caller supplies no revision, plan, NodeID, approval hash or recovery
filename. `--project-directory` can select the same deployment when invoking an
entry stored elsewhere. Python does not install or replace Gin's global `foc`.

`protocol/python-install.json` explicitly identifies install support. A release
with only the older upgrade protocol returns `source_install_missing` before
building or creating resources. New installation executes its selected source;
retry executes the indexed source without selecting a newer version. Its role is
read from the journal, so Storage needs no repeated selector. Another Compose
project in the same working directory refuses before build, and the exclusion
is repeated before resource creation and startup. Existing files and containers
remain intact. Once installed, `upgrade` first enters that installation's retained
source before live discovery; an identical release returns `already_current`.

Fresh installation uses the preparation library `operator_install_package.py`,
followed by the resource/startup libraries. Preparation alone observes a
pristine control directory, local volume names, project/container names, mounts
and ports before building. Existing data, host receipts, occupied volumes or
different canonical Compose files refuse without replacement. Canonical files
are only planned, and `.env` is retained byte-for-byte. Images build from the
verified stage and every application/engine image is pinned to its actual ID.
Source, inputs and empty-resource observations repeat before a package receipt
is published. Build failure retains its private intent and diagnostics. This
stage never starts a service, creates a data directory/volume or initializes SQL.

Install V4 passed 220 focused cases in 22.628 seconds, four source policies and
four real package preparations in 32.773 seconds, without skips: business SQLite,
business MySQL, Direct and Relay. Each uses canonical Dockerfiles and actual
engine pulls, then reloads the pinned package. All preserve private inputs and
leave no project containers, volumes, data roots or copied canonical files. All
566 V4 remote files and four actual build inventories/modes match their source
proof. The fixtures explicitly isolate names/ports and admit local source; a
normal public package load refuses that source. These are preparation results,
not started-business, database, ACME, playback or complete-install acceptance.
Earlier fixture failures and actual inputs/results remain in the private
`operator-install-package-progress-evidence.json`.

`operator_install_resources.py` adds a separate installation reference and durable
resource journal. Before mutation it binds the package, public proof, private
inputs and project directory. Canonical files publish by atomic no-replace rename;
different owner files remain intact. The control inode and plain local volumes
are bound to this installation. Docker volume creation includes the project,
logical-volume and installation token atomically, so a lost successful reply
can reconcile the same volume. Unknown ownership, changed roots, foreign mounts,
unexpected contents or private-input changes refuse without deleting data.
Pre-start journal states require `business_may_have_written=false`; they cannot
reinterpret a possible business write boundary as empty-resource preparation.
Completed resource preparation is idempotent and still says services are stopped
and installation is incomplete. No old deployment or snapshot facts are forged.

Resource V3 passed 235 focused cases in 22.986 seconds, four source policies and
four actual owned-resource cases in 76.937 seconds. Business SQLite/MySQL and
Direct/Relay each lose a real successful volume-create reply, load their journal
in a new instance, reuse the same inode and complete/duplicate resource preparation.
Private env bytes stay unchanged. No Web, SQL or certificate process starts.
Fixture cleanup removes only exact token-owned empty unused volumes; four project
container/volume sets are empty afterward, while verified canonical files, journals
and empty control roots remain as evidence.

`operator_install_start.py` now starts that pinned package through normal Compose
and verifies the initialized database, node/key baseline and ordinary HTTPS signed
identity using read-only helpers. It records possible business writes before the
first `up`; retries cannot return to empty-resource creation, restore old state or
force-recreate a partial installation. Actual container hashes, image IDs, mounts,
ports and effective environment must still match the retained package. A durable
`source_install` receipt lets ordinary discovery recognize the completed package.
If publication succeeds but its reply is lost, retry reconciles the exact receipt
without restarting or overwriting new business writes. Unknown receipts and
changed helper ownership refuse while retaining their resources.

Start V5 passed 281 focused cases in 22.739 seconds, four policies and four actual
trusted-HTTPS first-start profiles in 454.611 seconds: business SQLite/MySQL and
Direct/Relay. Each loses an actual successful Compose reply and a published-current
reply. Business profiles log in, upload and read Range 206; their uploaded bytes
remain after retry. The newly installed MySQL profile then completes a further
upgrade and reuses its original session for Range 206. Its two declared local
revisions contain the same candidate application source, not two public releases.
All 572 source files and five actual build inventories match. Four projects and
15 helpers leave no containers/volumes/networks; each one-CPU Web returns below
0.7% for five consecutive samples. Exact host inputs and 93 paired child argument
arrays/results are indexed in private `operator-install-start-progress-evidence.json`.
Earlier failures are retained there, including a private-root Nginx read failure
and an incorrect Storage proxy-trust fixture; neither is counted as acceptance.

Normal business initialization now adds only root search bits needed by the
separate Nginx UID; it does not broaden file read/write permissions. Native Storage
keeps private root permissions and defaults to no reverse-proxy trust, with explicit
proxy configuration still supported.

Start V6 adds actual installed-origin SQLite/MySQL controlled recovery in 413.008
seconds, with 281 focused cases in 22.797 seconds and four policies. Both capture
state after a fresh installation, prepare the candidate database, restore into
empty replacement resources and reopen the pinned original package. The failed
control root remains, media directories move with their inode intact, and original
sessions read Range 206 after trusted HTTPS recovery. This is generation-7 physical
state recovery between two local revisions of the same application source; it
does not demonstrate or authorize implicit DDL rollback. The runtime is unchanged
from V5. All 572 files and four build inventories match V6; both projects and 16
helpers leave no containers/volumes/networks. Sixty-six further paired inputs/results
and exact host inputs are indexed in `operator-install-start-and-recovery-evidence.json`.
These V6 library results precede the installation entry acceptance below.

Entry V4 passed 299 focused cases in 22.842 seconds, four source policies and five
actual Docker-host cases in 400.094 seconds. Business SQLite/MySQL and Direct/Relay
run the real CLI parser and source handoff with actual `os.exec`. Each host exits
after successful Compose startup and again after current-receipt publication;
new invocations resume, preserve the container IDs and return the same completed
installation. Original-session Range 206 retains new business uploads. Repeated
same-release `upgrade` returns `already_current` without rebuilding or restarting.
The fifth case retains a stopped foreign-project container and refuses the shared
directory before image build/pull, data or package creation; only the fixture
removes that container after the assertions.

All 576 files and eight template/retained-source inventories match V4. Four
installation projects, eight helpers and the conflict fixture leave no containers,
volumes or networks. The private `operator-install-entry-progress-evidence.json`
indexes 102 paired child inputs/results, 20 actual source exec inputs and 40 real
CLI call inputs. Local-source/private-CA admission and expected host exit codes
35/36 are explicit fixtures; normal public staging rejects the local source.
Genuine published-source proof, exact-final-source full gates and fixed-role fleet
upgrade/recovery remain required, with all other PY01–PY18 requirements.

The terminal-entry compatibility gap is fixed: a completed managed deployment
first executes its retained verified source with `select_upgrade`, before an old
original entry attempts live discovery. That selector chooses the next published
source and passes the same inherited directory lock through the next exec. A
pending operation continues its old source instead. Selection V2 passed 235
focused cases in 22.580 seconds, four source policies and two application cases
in 521.092 seconds, comprising three actual upgrade transactions. MySQL first
adds its missing `media-init`, then a simulated old discovery limitation must
handoff before discovery; both a no-op and a complete second upgrade pass. The
two declared local revisions have the same candidate application source; they
test replacement/orchestration and image/source binding, not a second feature
change or genuine public commits. Original keys/session Range and new uploads
survive. The private CA and local-source admission remain explicit fixtures.

All 569 Selection V2 remote files and its four actual first/template/retained
build inventories/modes match the source proof. Both application projects and
23 recorded helpers leave no containers or volumes. Resource V3 separately
matches its 569-file source and four owned-resource profiles. Inputs/results,
one-way exec records, source scopes and earlier snapshots are indexed in
`operator-selection-and-resource-progress-evidence.json` outside the repository.
Genuine published-current-source, full fixed-role and exact-final-suite acceptance
remain required, together with the rest of the Python renewal.

Before an owned fence exists, recovery cancels the journal without restarting
services. Before database preparation or a completed snapshot, it rejoins the
original package using the original roots and volume names. It retains incomplete
backup files without restoring from them. Once database preparation may have
started, recovery requires the verified snapshot and empty replacement resources.
After the persisted opening boundary, it refuses old-state restoration because
new business writes may exist. Original-resource recovery checks the existing
schema, node and keys read-only; only that recovery identity check permits the
old runtime to resume its own unfinished uploads. Upgrade preflight remains strict.

Host failures distinguish exhausted space, permission, missing material, read-only
filesystems, cross-device moves, occupied destinations and I/O timeout. Failed
`upgrade`/`resume`/`recover` also report the recorded phase without changing the journal or
reconciling containers. Raw host paths and exception messages are not returned.

`operator_deployment.py` validates the local daemon, Compose labels, effective
private inputs, actual service environment, fixed published ports, mounts,
local-volume ownership and running image IDs. Discovery is repeated before it
returns. Inactive MySQL definitions are not mistaken for an active database;
the old `mysql` profile is recovered only when Web actually uses MySQL and its
existing project container is present. Storage stays independent. Unknown services/mounts, foreign mounted containers,
unreproducible old shell inputs and configuration/container changes refuse.
Health timestamps and Docker mount-list order are observations, not deployment
identity changes. Candidate mapping admission is a library operation only: it
does not start admitted services or create their volumes.

`operator_release.py` selects the newest canonical `main` without a version
argument. It binds its unique same-repository merged promotion, exact source
tree, newest dev push CI, main promotion CI and each required job in the exact
workflow attempt. It rechecks runs and `main`; an older successful run cannot
hide a newer failure. A missing merge SHA in archived GitHub PR metadata is
accepted only with a full merged PR plus exact ordered Git merge parents. An
equal tree alone does not establish that association.

| Observation | Result |
| --- | --- |
| Dev CI is queued/running | `source_ci_pending`, waiting, exit 3 |
| Main promotion CI is queued/running | `promotion_ci_pending`, waiting, exit 3 |
| Exact CI finished unsuccessfully | `source_ci_failed` or `promotion_ci_failed` |
| Exact run/job/promotion is missing | Separate missing/incomplete proof code |
| API throttling, timeout, TLS failure or denied access | Separate transport/access code |
| Proof changes while being read | Refusal; no version guessed or activated |

The implementation follows GitHub's [workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs)
and [commit-associated PR API](https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit).
Public merged-source/CI proof is separate from this renewal's still-required
code review and exact-final-source acceptance.

`operator_source.py` stages into a new private directory. It reconstructs the
selected Git tree, verifies every archive file against its Git blob and records
SHA-256 inventory. Links, submodules, traversal, extra/missing files, corrupt
bytes, oversized headers/expanded data and modified staging refuse. Source
directories/files retain build-readable modes even under host umask 077; the
outer staging directory and completion receipt remain private. Public proof is
repeated before completion. Partial failed staging is retained without a
completion receipt. This library is not a permission to activate its source.

On 2026-10-20, source V10 passed 83 focused cases in 0.522 seconds, seven real
Docker-host layout cases in 117.575 seconds and four source policies. Actual
command argument arrays, timestamps and exit codes are retained in the native
JSONL input record, including the direct host entry invocation. The native
stand-ins sleep: they prove real labels, mounts, inputs,
images and actual host CLI invocation, not old-application database upgrades or
business/TLS readiness. Real MySQL is used for its mount/image observation.

V8 separately verified public PR 74, exact dev/main CI and the existing published
`5da7a21` tree. Actual ordinary-HTTPS staging and local revalidation passed for
381 files, 3,044,958 source bytes and a 774,359-byte archive; no source was
executed or image built. That published baseline proof is not a public release
of the current uncommitted overlay. The V8 public-provenance/staging modules
are byte-identical in V10. Earlier failed inputs, source identities
and successful reruns are retained in `operator-source-progress-evidence.json`
outside the repository.

### Prepared images and owned cold capture candidate

`operator_prepare.py` now renders the candidate against the original project
directory and the same active overrides/env files. Relative data and private
env-file paths therefore keep their original meaning; build contexts are then
replaced explicitly with verified source. Both ordinary backup and operator
discovery decode Compose's reloadable dollar escaping; generated Compose encodes
literal values again. Actual Docker tests include dollar signs in private values
and data paths, rather than assuming a JSON round trip preserves them.

Preparation retains old endpoints, limits, top-level network options and
logical/actual volume identities. A new network requires an explicit mapping.
Unknown service semantics, persistent additions and unadmitted temporary volumes
refuse before building. New sockets are checked against Docker and host listeners.
Application images use operation-specific names and source labels; the activation
file contains exact image IDs and no build instructions. Database/cache images
remain their actually running IDs. A changed old tag cannot change the retained
old-image recovery file. Actual command inputs precede execution, and private
build diagnostics remain available after a failure. Preparation never starts a
new service or temporary volume and does not stop the old deployment.

`operator_upgrade.py` currently joins these durable library phases:

```text
images_prepared -> fencing -> fenced -> stopping -> cold -> backing_up -> backed_up
  -> database_inspecting -> database_preparing -> database_prepared
  -> activating -> active_fenced
  -> opening_stopping -> opening_stopped -> opening -> opened
```

The operation binds prepared receipts, exact images, Compose files and persistent
root inodes before owning a fence. It refuses another manual/operator fence.
Fence publication uses complete material plus a no-replace hard link; restart
reconciles its own known extra link and leaves one link before cold backup.
Interrupted temporary material is retained, not overwritten. Every state change
has a private checksum, revision, atomic replacement and directory barrier.
Old revisions cannot record new progress. Cold capture joins all project writers,
rechecks host/container writers and validates the existing state-only snapshot.
An incomplete snapshot is retained while resume captures a separate new one;
a completed corrupt snapshot cannot become a successful result. A lost completion
record reuses its verified archive. Repeated capture does not copy payloads or
restart services. Actual Docker inputs/results are recorded separately from status.

Native cold cases bootstrap generation 4 using the exact published Python
`5da7a21` tracked source and create real node/vault/premaster state. SQLite table
facts are checked against the snapshot. MySQL additionally restores the cold set
into separate empty resources with the exact server image and compares every
typed application table plus account authentication facts. Both retain media and
recording bytes in their original fixture roots; those bytes are absent from the
snapshot. These are old-source database bootstrap/capture/recovery cases. They
do not start the old ASGI application or prove a complete application upgrade.
The historical V16 cases did not migrate generation 4 to 7. Native candidate image cases use declared local
acceptance sources; they do not forge public release provenance.

Database preparation uses the pinned candidate Web image as a short Compose
one-off with no service ports or Docker socket. Only the existing pinned MySQL
service may run alongside it. Source, Compose, snapshot and root identities are
rechecked first. The host durably saves a private typed-table baseline before
permitting DDL; the helper receives only that file through a read-only bind.
It holds the runtime's exclusive DATA_ROOT lease, validates the owned hard
fence, opens existing identity/vault/premaster material, and never invokes node
or key initialization. SQLite inspection uses a separate mode=ro connection
without WAL-changing adapter callbacks. MySQL inspection is a read-only
transaction. Row fingerprints stream with constant table-size memory, preserve
types and duplicates, and compare every original table/column after formal
migration. Only generation/history additions and newly introduced tables are
allowed. A partial forward migration or lost successful helper reply keeps the
original baseline and fence for a verified retry. Compose progress messages are
not mistaken for the helper's single structured result.

The cold helper refuses pending publication/delete journals, uploads,
reservations, migration and Backup work. Historical compute rows are retained
without reviving execution or treating old leases as live work. The startup
media recovery path now returns before publication or deletion recovery while
the hard fence is active; regression cases retain real ciphertext, publication
proofs and quarantined delete material, then resume ordinary recovery.

The live preflight now runs before journal creation and again before publishing
the hard fence. It imports the candidate configuration and checks existing node,
vault, premaster, schema/history and lifecycle state through read-only database
transactions. Its one-off mounts only the old Web resources, all read-only,
declares only those existing volumes, and opens no service ports. It does not
claim the running Web's exclusive inode lease or fingerprint whole tables while
normal writes are active. Directory observation streams through the same bounded
depth as managed cleanup, including hidden directories, and refuses partial
uploads, recordings, publication proofs or delete quarantine without deleting
them. Cold preparation repeats the gates and binds identity/keys/generation to
the live preflight. A later race which leaves new work after fencing still
requires controlled old-version reopening; it is not permission to ignore work.

Driver failures retain bounded classifications for authentication, permission,
missing database, connection loss, lock timeout and deadlock, including the
numeric driver code. Wrapped migration exceptions preserve that classification.
Configuration errors omit raw input values. Unknown exceptions still return a
class name rather than potentially exposing SQL/credentials; complete failure
recovery and operator diagnostics remain release gates.

Fenced Web startup validates an existing current database through a read-only
connection and does not run bootstrap or migrations. It loads existing node and
premaster keys without insertion or regeneration, and skips Master Local
adoption and lyric backfill. IP cache hydration may rebuild Redis, while SQL
projection publication and its retry loop wait for the fence to clear. Pending
publication/delete recovery and upload cleanup remain inactive under the hard
fence. A fenced Storage process cannot promote a Standalone identity. These
startup guards do not authorize opening.

`activate_fenced()` starts the complete pinned Compose package with no build or
pull. It recreates services so engine references and configuration labels also
match the retained package, then checks actual inputs, mounts, original root
identities and image IDs. A read-only live helper compares every original
database table projection and the existing identity/keys against the durable
cold baseline. An interrupted activation may reconcile under the same fence;
repeating an already verified activation does not recreate services. Prepared
observation is explicitly bound to its private receipt and exact configuration;
it is not a general permission to trust arbitrary generated Compose files.

One-off helpers have unique recorded names and ownership tokens. Host command
completion or timeout joins the daemon-side container lifetime as well. Only an
exact name, token, project, one-off label, image and AutoRemove binding may be
stopped; unknown containers remain intact. Retained helper records permit
reconciliation on transaction resume. The separate private operation index is
loaded before ordinary live discovery, so a recorded daemon-side helper or a
partly replaced service roster does not prevent controlled resume. It binds the
project, original private inputs, preparation hash and journal identity.

`operator_registry.py` now publishes a private host receipt for the generated
Compose file. Default discovery validates that receipt, its preparation/image
binding, original root identities and original Compose/override/env inputs.
It reports `active_fenced`, `opening` or `ready` separately from container
health. The original source-file mapping is retained for the next preparation;
the generated private `compose.json` is never guessed to be a repository base.
Changed or newly added implicit `.env` inputs refuse an unapproved configuration
mapping instead of being silently ignored. The previous registry body/hash is
retained in the next transaction. Actual application V8 passed both engines and
the real host entry's default rediscovery in 341.657 seconds; its 74 focused
cases passed separately.

`open()` verifies the fenced endpoint before joining Web and its gateway. It
persists the one-way opening boundary before removing only its owned fence,
then starts those services normally so ordinary startup initialization runs.
Readiness, system-trusted TLS and a fresh challenge-bound original signed
identity are checked again. HTTP-only Standalone retains its narrower HTTP and
database identity proof; it never claims certificate or signed-identity proof.
The probe reports that actual media reads remain unverified by this generic
entry. Native representative Range checks supply separate evidence.

Once opening is persisted, old-state restoration is forbidden even after a
lost reply. Retrying may finish normal startup/verification, but it does not
apply an old SQL snapshot over new writes. A completed operation returns its
recorded result without restarting services or clearing a new manual fence.
Post-opening identity observation permits new business transactions; the
unchanged pre-upgrade idle gates still refuse those same transactions.

At the initial opening snapshot, new-install orchestration was still pending;
Entry V4 above supplies its later local acceptance. The new-upgrade candidate
has the local staged application evidence below. Early abort before a
verified recovery snapshot is implemented and has the native evidence below.
Full canonical cluster upgrades and the
complete activation/opening/failure matrix remain release gates. Post-opening writes cannot
be overwritten by this cold set. Public use still requires a release containing
this implementation and its required exact-source acceptance.
The acceptance gates below remain required. No production, Gin deployment or
owner S3 preview is used by these fixtures.

Application V24 passed 143 focused cases in 24.751 seconds, four source policies
and two actual early-abort cases in 430.321 seconds. SQLite and MySQL each cancel
an unfenced operation without restart, then inject ENOSPC after writing a real
control archive during a second operation. No filesystem was actually filled.
The incomplete backup remains unchanged and is never used for restoration.
Recovery reuses original roots, volumes and payload inodes, opens the pinned
generation-4 package with trusted test-CA HTTPS and signed original identity,
then retains a real new API upload across host-process death before the result
is saved. The old session still reads Range 206. Default host `inspect` works;
four fixture projects and 24 recorded helper names leave no resources.

All 559 V24 remote source files and both actual candidate build inventories/modes
match the recorded source proof. The preceding V23 early-abort pair separately
passed in 420.916 seconds. Native positive recovery uses the explicit internal
local-fixture source boundary. Public default `resume` and `recover` are actually
tested to refuse that unpublished source before fencing. These results do not
claim a positive public-release recovery or a complete exact-final-source suite.
Actual host inputs, paired step results, hashes and retained failures are indexed
in `operator-early-abort-progress-evidence.json` outside the repository.

Handoff V2 passed 200 focused cases in 22.844 seconds and four source policies.
A real isolated host `exec` preserves its process ID and keeps a competing entry
locked out until exit. Actual staged application continuation then passed both
Standalone engines in 383.711 seconds: published generation 4 to candidate 7,
old session Range 206, trusted test-CA HTTPS and signed original identity. MySQL
starts from an old layout missing `media-init` and safely adds that service.
Each case loses the host reply after opening, makes a new API upload and retains
it when another staged process resumes. The default public bootstrap refuses
the unpublished fixture source before selecting a different release.

The native continuation explicitly admits local source and private CA wrappers
through its isolated test driver; it does not exercise a successful public
release selection. All 563 source files and both actual build inventories/modes
match V2. Both projects and 16 helper names leave no containers or volumes.
Actual host inputs, paired command results and source identities are indexed in
`operator-handoff-progress-evidence.json` outside the repository. V2 did not prove
new installs, fixed roles, the entire final native suite or a genuine current
public release; later installation evidence is recorded above.

Handoff V3 passed 209 focused cases in 22.752 seconds, four source policies and
two actual staged-recovery cases in 443.044 seconds. SQLite and MySQL each use
`recover` to cancel before fencing without restarting, then to reopen original
resources after injected ENOSPC follows a real control-archive write. After a
lost opening reply and new API upload, a separately staged `resume` finishes
reconciliation and a duplicate `recover` returns the same result. Generation 4,
original roots/volume names, payload inode/hash and original-session Range 206
are retained; no backup restore or new volume is used. The filesystem was not
actually filled. Ordinary HTTPS verifies the private fixture CA and original
signed identity. Public default `resume`/`recover` refuse unpublished source.

All 563 V3 remote source files and both actual build inventories/modes match
their source proof. Two projects and 12 recorded helper names leave no containers
or volumes. Typed errors retain the requested recovery command. Read-only phase
observation accepts a future phase without executing it, while malformed state
refuses. The private `operator-auxiliary-source-evidence.json` indexes actual
inputs/results and the explicit local-source/test-CA boundary. It does not claim
fresh installation, fixed-role/full-suite acceptance or a genuine current public
release.

The live-preflight V3 snapshot passed 96 focused cases, three actual Docker
cases and four source policies. The native cases preserve running old services
when an actual partial file or invalid candidate configuration refuses; no hard
fence, upgrade state or new temporary volume is created by that failed preflight.
Those old services are layout stand-ins; their database/key initialization uses
the exact published generation-4 source, not a started old ASGI application.

Startup V4 passed 95 focused cases (one dedicated CI-MySQL case skipped), two
real candidate-ASGI cases and four source policies. SQLite and MySQL each use
the published old source for generation-4 database/key initialization, prepare
generation 7 through the host transaction, then actually run the candidate Web
with one worker, one CPU and 512 MiB. Ordinary verified HTTPS uses a private
test CA inside that fixture; the client checks the served leaf fingerprint,
signed existing identity, readiness 200 and business refusal 503. All 37
original SQLite tables or 36 original MySQL application tables, keys and payload
fixture hashes remain unchanged. Five consecutive CPU samples recover below
20% of one core. Cleanup waits for the exact AutoRemove container to disappear
before removing its unused owned temporary volume. V1/V2/V3 failures, command
inputs and successful rerun are retained in
`operator-preflight-startup-progress-evidence.json` outside the repository.

Those startup-V4 Web images are explicit local component fixtures, not the canonical
published deployment images. The old ASGI application is not started in these
cases, and product ACME issuance/renewal is not exercised by the test CA. The
separate application-V4 case now builds the exact published old source and the
candidate with their real Web/Nginx Dockerfiles. It starts the old SQLite
application, uses ordinary verified HTTPS for Admin login, uploads a real audio
sample and checks Range bytes against the original. The host captures state,
prepares generation 4 to 7 and starts the complete candidate package. Existing
rows/keys and original payload hashes remain intact; HTTPS readiness returns
200 while business remains fenced with 503. The 163.432-second case passed.
Only its isolated private CA trust is added in explicit test wrappers, and its
old trusted-proxy network is the fixture's actual Docker bridge. This is not
public-image, public-CA or opened-business evidence. At that V4 snapshot,
MySQL and a legacy layout missing an init service were still untested, in
addition to failure recovery, owned opening and ordinary rediscovery.

Application V6 subsequently passed both default SQLite and MySQL deployments
in 287.748 seconds. Application V7 passed the default SQLite deployment plus
a MySQL legacy layout missing `media-init` in 338.241 seconds. V7 actually
adds/starts that admitted init service against the original data root. Both
engines retain every original table projection (37 SQLite tables or 36 MySQL
application tables), node/key material and the uploaded sample hash. Both
repeat activation without replacing service containers and recover five
consecutive low-CPU samples within the unchanged deadline.

V7 also kills the real host Compose client after a five-second timeout while
its uniquely named helper is demonstrably still running in Docker. The host
transaction joins that exact container's stop/removal before proceeding; it
does not publish a fence during this preflight fault or delete business data.
The fault is explicitly fixture-only and its signal/input is recorded. V7's
67 focused cases and four source policies passed. The later wiring snapshot
adds a helper-record binding regression and passes 101 focused operator,
repository and deployment cases in 23.091 seconds, all four policies, native
script Bash syntax and CI artifact-collector Bash syntax. Actual-application
cases are now wired into the complete operator gate; only their sanitized
summaries/input records and test log are selected for CI artifacts.

The private `operator-application-progress-evidence.json` indexes immutable
inputs, outputs, source proofs, successful cases and earlier failed attempts.
None of these cases opens business, exercises an existing fixed-role cluster
upgrade or restores a failed mutated deployment. Those gates remain pending;
the host CLI still cannot advertise a complete upgrade. Later opening candidates
are separate from that historical V7 evidence and require their own acceptance.

Application V13 passed 99 focused cases in 23.023 seconds, both actual
SQLite/MySQL application upgrades in 435.217 seconds, and all four source
policies. Both cases now reach ordinary opened business, repeat generic
discovery and preserve a real API upload made after an injected lost opening
completion. The pre-upgrade authenticated session still reads the original
sample with HTTP 206 and matching Range bytes. Duplicate completion preserves
the new file and does not recreate services. MySQL still exercises the legacy
missing-init mapping; each one-worker Web retains its one-CPU/512-MiB limits
and recovers five consecutive low-CPU samples.

That acceptance found and fixed two real observation boundaries. A probe invoked
with `python -m` now shares the importable helper's error type, so a known
refusal retains its specific code instead of becoming `PreparationError`.
Nginx and ASGI now agree on the exact read-only signed identity exception under
the hard fence: one GET with exactly one lowercase 32-hex challenge. Real POST,
HEAD, extra-argument, missing and invalid challenges remain fenced. No other
internal operation is opened. The V10 failure specifically records identity
HTTP 503; this was a fence mismatch, not a certificate failure.

Container inventory also tolerates a proved disappearance between `docker ps`
and `inspect`, with at most three complete re-observations. Stable inspection
failures, invalid inventory and continuous churn still refuse. Each V13 case
actually removes an owned temporary container in that gap and proves a still
live unrelated sentinel and the old Web remain in the complete observation;
partial failed inspect output is not accepted. Docker container/image/volume
inspection errors retain separate safe codes. V9/V10/V11/V12 failures are kept
with the successful source and actual inputs in the private
`operator-opening-progress-evidence.json`.

These are local candidate Standalone fixtures with explicit test-CA wrappers,
not public-release or fixed-role fleet acceptance. They establish the private
forward transaction through opening. Application V14 adds independently loaded
host-process recovery, with 106 focused cases in 21.733 seconds, both actual
application upgrades in 520.201 seconds, and all four source policies. A killed
host process leaves a real daemon helper running; another process finds the
private operation index, verifies retained source/configuration/pinned images,
joins only that owned helper, and continues database preparation and activation.
A separate host death after opening is recovered without overwriting a new real
API upload. Public `resume` rejects the unpublished source before fencing.
Fenced endpoint verification repeats all original typed table projections;
open endpoint verification permits new business transactions. Actual pre-upgrade
session Range reads and one-CPU/512-MiB Web recovery pass for both engines.
Exact inputs, source proof, logs and results are retained separately from V13 in
the private `operator-application-collected-v14.json`.

The new `operator_recovery.py` candidate prepares a fenced old-state replacement.
It retains failed control roots at their original paths and creates sibling
replacement control roots plus newly named owned volumes. This avoids moving the
deployment's original root or overwriting occupied destinations. The existing
empty-target restore validates the original state-only snapshot and exact old
engine images. Interrupted state restores are retained, then retried only into
fresh empty destinations. Excluded media/recordings use a journaled same-filesystem
no-replace directory rename, preserving their inodes without copying payloads.
Linux nested mounts, foreign leases, changed roots, competing destinations and
manual fences refuse. Selecting recovery prevents forward upgrade resume; the
persisted opening boundary always forbids old-state restore. This library does
not start or open the recovered business services. Application recovery V16
passed 113 focused cases in 21.848 seconds, both real SQLite/MySQL recovery cases
in 534.113 seconds and all four source policies. Each case first activates the
generation-7 candidate under its owned fence, then selects controlled recovery.
An independent host process exits immediately after an actual payload rename;
another process loads the same journal and finishes without another payload copy
or state restore. Original file inode/hash and every failed persistent-root
identity are retained. The restored MySQL engine is actually started alone;
read-only preflight for both engines exactly matches the original generation-4
identity/key/role facts. It is stopped again before duplicate reconciliation.
Recovered Web/gateway startup and opening/recognition were not implemented in
that snapshot; their later candidate and evidence are described below.
V15's initial fixture failure is retained: its synthetic failed SQLite update
was not committed; fixing that fixture did not change recovery behavior.
V16 source/inputs/results are in `operator-application-collected-v16.json`.
Application V17 then passed 115 focused cases in 25.383 seconds, four source
policies and four actual application cases on the same runtime/test source.
The SQLite/MySQL forward pair passed in 557.958 seconds and the independently
isolated recovery pair in 546.719 seconds. Recovery also checks changed restored
volume ownership, new active writers and phase binding; unused volume definitions
are not created. Read-only cleanup verifies six V16/V17 application projects have
no containers or volumes, all 70 recorded helper names are absent, and no volume
retains any of the four recovery tokens. The private
`operator-recovery-progress-evidence.json` indexes 559 V17 artifacts, 140 matched
step input/result pairs, earlier V15/V16 evidence and actual host invocations.
It verifies 551 current files against the V17 Linux source proof; the only later
changes are three documentation updates. This remains candidate Standalone
acceptance, not restored business opening or final-release acceptance.
At that snapshot, new-upgrade host orchestration was unfinished; its later
candidate is described above. Fixed-role fleet acceptance and final exact-source
checks remain unfinished.
The later `operator_recovery_open.py` candidate now implements restored-service
opening. It first joins every project writer, validates the exact restored
generation/identity/keys with the candidate's read-only helper and stops the old
engine again. Legacy Nginx cannot expose its signed identity under the hard
fence, so this path does not weaken that old gateway or reuse a generation-7
fenced endpoint assertion. It persists the irreversible opening boundary before
publishing the replacement mapping or unlinking its restored fence, then starts
the pinned old package normally. An explicit historical-generation probe checks
ordinary trusted HTTPS, a fresh signed identity and business readiness; it never
initializes/migrates the restored database. New writes are allowed after opening.

The restored receipt has its own `restored_original` origin, bound to the failed
upgrade, verified snapshot mapping, old image IDs, original private inputs and
replacement roots. It never claims the old images came from the new candidate's
source. Default discovery can recognize that receipt, and a completed recovery
can be the predecessor of a subsequent upgrade. New temporary namespaces use
operation-specific names; failed or unadmitted old volumes are neither reused
nor cleared. Only an exact recorded, stopped, admitted extra service container
may be removed, without `--force` or volume removal. A lost final opening reply
cannot downgrade a ready receipt or restore older state.

Opening acceptance now passes 128 focused cases in 23.465 seconds and four
source policies. Application V21 MySQL passes in 426.493 seconds; the identical
source's SQLite case passes sequentially in V22 in 332.683 seconds. Both restore
the original generation-4 package after fenced generation-7 activation, preserve
failed roots and payload inodes, actually open trusted HTTPS and verify the
original signed identity. Each kills the host process after final endpoint
verification, uploads a new file through the recovered old API, resumes and
repeats completion without losing it. The original authenticated session still
reads matching Range bytes with HTTP 206. Default host CLI discovery recognizes
the recovered receipt, and another verified candidate preparation plus operation
admission succeeds with those replacement roots. One-CPU/512-MiB Web recovery
passes for both engines.

The V19 failed opening is retained: Compose reused unchanged services whose
labels still named the failed package's inputs. The fix explicitly creates all
stopped restored containers before the irreversible boundary, then verifies
their actual images/mounts/roots. It never force-recreates services on a retry
after possible new writes. V20 separately passed both recovery cases in
744.685 seconds. V21's parallel SQLite fixture refused during initial read-only
discovery because the global Docker inventory kept changing; no upgrade/fence
had begun. V22 reruns the same immutable source sequentially, without relaxing
the three-observation limit or any timeout.

Read-only cleanup finds no containers/volumes for nine V19-V22 fixture projects,
all 110 recorded helper names absent and no volumes carrying six recovery
tokens. All 556 remote source files and their modes/bytes in both actual build
stages match the retained V21 proof. Actual host commands, paired step inputs
and results, source inventories and prior failures are indexed in the private
`operator-restored-opening-progress-evidence.json`. These are Standalone
candidate cases. Those snapshots did not prove new install/upgrade/recovery-selection
orchestration, early failure abort, fixed-role fleets or final complete-source
acceptance. Later entry and early-abort evidence is recorded above.

None of these steps closes the full PY01-PY18 renewal or owner S3 acceptance.

The separate database V7 snapshot passed 91 focused cases in 23.888 seconds,
two real host/Compose transactions in 147.796 seconds and all four source
policies. Each transaction uses the published generation-4 source for old
database/node/key initialization, then the candidate helper image to reach
generation 7. SQLite and real MySQL preserve all original typed table facts and
keys. Both inject a lost host reply after successful database preparation and
resume from the same durable baseline. Fixtures are Standalone database/component
cases, not a started old/new Master/Follower application or full TLS upgrade.
Their original payload fixtures remain outside the state snapshot. Owned unused
temporary volumes are reclaimed after the tests. V2/V3/V4 failures and exact
inputs remain with `operator-database-progress-evidence.json`; V1's direct
helper cases are separate evidence, not relabeled as host transactions.

The V16 snapshot passed 101 focused cases in 0.737 seconds, 13 actual Docker
cases in 314.191 seconds and all four source policies. It includes the earlier
discovery/image cases plus both generation-4 database cases: 37 SQLite tables
and 36 MySQL application tables. MySQL account authentication facts also match
after the separate empty-target restore. The native input trace includes the
host entry and 58 paired Docker inputs/results from owned cold capture; build,
Git archive and old-code bootstrap inputs are retained before execution.
Original failed test inputs remain alongside their fixes. The private evidence
index is `operator-prepare-capture-evidence.json` outside the repository.

The new native cases are wired into the operator acceptance script, with only
sanitized summaries/input logs selected for CI artifacts. Whole-pipeline CI
composition and runtime budgets still need revalidation; this local snapshot
does not claim GitHub CI or final release acceptance. No upgrade command is
exposed by these preparation/capture results.

## Authority and source delivery

The operator is an explicitly invoked Linux-host Python program using the local
Docker CLI and ordinary Compose inputs. This extends the existing offline
`scripts/operator_backup.py` boundary. Product Web containers gain no Docker
socket, SSH authority, host services or background deployment controller.
The install entry and documented business/Storage paths all invoke Compose.
No global Gin `foc` command is replaced.

The owner explicitly confirmed this execution boundary on 2026-10-20: retain
the host's one-shot Python entry. Compose remains the deployment authority;
do not grant its Docker socket to a new operator or business container.

The candidate source must be the Python repository's published `main` commit.
Before selection, verify its unique same-repository merged `dev -> main`
association, identical reviewed/main Git trees, successful exact-source `dev`
checks and successful main promotion check. Pending CI, failed CI, missing
provenance, rate limiting and transport failure are distinct preflight states.
An unavailable proof cannot be converted into a successful update or guessed
version. A clean local reviewed source may be used for isolated native acceptance;
that injected candidate is not evidence of public release verification.

Retrieve and stage source before fencing business traffic. Retain its exact
commit, file inventory and hashes. Reject path traversal, links, special files,
unexpected archive roots and a modified candidate. Python Web/Nginx images can
be built through Compose from this staged source; CI does not have to publish a
new Web image for every business change. Record exact resulting image IDs and
platforms before activation, using distinct image names so another local project
cannot accidentally consume a moved shared tag.

## Recognizing an existing deployment

Use explicit project-directory context plus actual Compose container labels:
project name, working directory, active configuration files and service name.
Render those same inputs and compare them with Docker's actual containers and
mounts. A directory name, sample `.env` or old README path is not deployment
identity. Multiple matching deployments, foreign writers, missing configuration
files and a changed mapping refuse automatic selection.

Supported original inputs are the business `docker-compose.yaml`, its optional
MySQL overlay and the independent `docker-compose.storage.yaml`. Select the
database from effective Web configuration and actual data mounts, not merely
from a present MySQL service definition or profile. Never enable MySQL for an
existing SQLite deployment or combine Storage and business Compose.

| Existing resource | Required mapping |
| --- | --- |
| Compose project and services | Preserve the existing project name and service identities; inspect name/port collisions before stopping anything |
| `.env` and active operator overrides | Preserve actual private inputs and their precedence; never replace them with repository examples or emit expanded secrets |
| `/app/data` | Preserve the exact existing bind source, including a custom `DATA_DIRECTORY`; reject broad, linked or overlapping roots |
| SQLite | Preserve its actual regular-file path within persistent data, including WAL/SHM and every application table |
| MySQL | Preserve its existing local volume and exact compatible server image; a new source build is not a database-engine upgrade |
| Runtime secrets and node vault | Preserve existing volume identities and bytes; no initialization of a second node |
| Certificate state/published generations | Preserve account, private keys and both private/published volumes as one recovery boundary |
| Redis | Preserve its existing data volume and recorded image identity |
| Media/recordings | Keep existing paths and bytes; exclude payloads from operator state snapshots |
| Temporary sockets/cache/traffic counters | Regenerate only declared ephemeral state; do not reinterpret an unknown mount as disposable |

Existing custom services or unknown persistent mounts require an explicit
mapping before an update can proceed. The operator must explain the unsupported
service or mount; it must not use `--remove-orphans`, discard overrides, initialize
a new data root or silently remove a service. The already approved Nginx traffic
runtime is a declared ephemeral addition, not a new business-data volume.
New-service acceptance must cover both a declared supported addition and refusal
of an unknown service, including failure before any new service starts.

## Durable upgrade phases

1. Read-only discovery records the old deployment, source identity, schema,
   relevant inputs, persistent mappings and existing image identities.
2. Stage and verify the candidate; render its Compose configuration against the
   existing inputs, verify mappings/permissions and prepare its images. Failure
   here leaves the existing deployment running and records the exact failed step.
3. Persist an operation journal and an operation-owned maintenance fence. Refuse
   a preexisting manual fence or another active operation rather than claiming
   ownership of it. Stop all project writers, including the certificate agent,
   and recheck foreign containers/host file descriptors.
4. Capture and verify a private state-only recovery snapshot through the existing
   backup tool. It includes SQL, identities, premaster, secrets, certificate state
   and relevant control volumes; it never copies media or recordings. Incomplete
   snapshots cannot authorize migration or restart.
5. Prepare the target database under the fence, preserving prior rows and
   validating formal migration history. Persist the schema result before
   activating services. Do not infer SQLite and MySQL DDL have identical rollback
   behavior.
6. Start only the declared target Compose services with the original project and
   persistent mappings. New services may start only after their mapping and
   recovery boundary have been admitted.
7. Verify database integrity/generation, node identity, expected service images,
   health, authenticated relationships and certificate-verified HTTPS. Record the
   scope of real representative business reads separately from a status report.
8. Publish the new deployment receipt and remove only this operation's own
   maintenance fence. Retain the old source, image identities, original inputs,
   operation journal and verified recovery material.

Every phase must survive process death and lost command output. Resume rechecks
the journal against the current deployment before executing another step. An
already completed operation returns its recorded result without changing new
writes, current configuration or another operation's state.

## Failure and restore

Before writers stop, failures are read-only preparation failures. After writers
stop, a failure leaves an accurate journal and the owned fence in place; it does
not reopen business traffic merely because some containers are healthy.
Restarting an old executable is allowed only when the database/control state is
proved unchanged and compatible. The operator cannot infer a DDL downgrade from
an older source revision.

After database mutation, preserve the failed state and verified recovery set.
Controlled restore uses empty, inactive compatible destinations and exact
recorded database/cache images through the existing restore validation. Never
delete an occupied deployment to make it appear restorable. The operator must
retain enough original mapping information to identify media which were excluded
from the state snapshot. Post-opening writes require a new explicit recovery
decision; an old snapshot never authorizes overwriting them.

The failed-upgrade mapping is deliberately separate from in-place DDL
rollback. With the operation still fenced and never opened, stop every project
writer and repeat the host/container lease checks. Retain the failed control
directory at its original path and record a new sibling destination on the same
filesystem. Allocate new
empty physical volumes with the original logical volume keys and project labels;
retain the occupied original volumes. Restore the verified old state into those
empty destinations using the exact old database/cache images. Media and
recordings move by same-filesystem directory rename into the restored control
root, preserving their inodes and bytes; they are not copied into the snapshot
or silently recreated. Every move needs durable before/after root identities and
restart reconciliation. Separate mounts, changed roots, foreign leases or
ambiguous material refuse. Admission must retain both old and replacement
resource mappings so old services can be recreated without guessing volume
names. The host library implements fenced preparation and restored opening,
with Standalone SQLite/MySQL native evidence. Early pre-snapshot abort now has
the separate V24 evidence above; the fixed-role matrix remains unfinished. The
existing empty-target restore alone does not authorize opening old services.

## Required native acceptance

Use an immutable old Python generation-4 deployment plus a separate candidate,
with SQLite and optional real MySQL. Compare typed database values, node/vault,
premaster, relationships, certificate state, quotas and original payload hashes.
Exercise default and custom data roots, actual named-volume ownership, a declared
ephemeral addition, unknown-service/mount rejection, changed inputs, duplicate
commands, concurrent operators and unsupported platform/daemon refusal.

Inject preparation/build failure, foreign writers, incomplete backup, SQLite
transaction failure, MySQL implicit-DDL interruption, service-start failure,
post-activation certificate/identity mismatch and process death at persisted
phase boundaries. Record exact inputs and recovery results. Re-run complete
source/native acceptance for the actual reviewed commit before release. A local
candidate, successful Compose render or copied source tree alone proves none of
the public provenance, upgrade or failed-recovery claims above.
