# Engineering and CI

`dev` is the only implementation branch; `main` accepts exactly one
same-repository `dev -> main` release PR. Do not create other branches, force-push,
or implement directly on `main`. After merge, fast-forward `dev` to `main`.
The owner reviews/authorizes release promotion.

The final runtime is Python/FastAPI, SQLAlchemy async, SQLite by default or the
original optional MySQL, Redis, native browser JavaScript and Compose. Business
HTTPS uses Nginx. Native Storage terminates TLS in Python. There is no Go backend,
privileged online updater, Python media encryption worker, or Compute Worker.
Nginx builds the approved small C live-traffic observer in a separate build
stage; no compiler toolchain or Go component is added to the runtime image.

Read the authoritative [architecture boundaries](../../ARCHITECTURE.md) and
[contribution procedure](../../CONTRIBUTING.md) before changing cross-cutting code.

## Source CI

The read-only `docker.yml` workflow installs pinned Python dependencies, checks
Python/JavaScript syntax, runtime CPU boundaries, deployment/env contracts and
English source comments, discovers the complete unit suite, and runs the browser
JavaScript smoke tests. Policy/JavaScript checks and four deterministic unit
shards each have a sixty-minute budget. Shards retain every discovered case,
including repeated IDs, and keep each module's fixtures together. The required
`source` aggregation independently repeats discovery and verifies that four
successful receipts cover the complete ordered shard lists from the same clean
commit and source-content hash. Missing, stale, failed or skipped jobs fail the
gate; dedicated test skips remain visible in receipts. This replaces a measured
64-minute serial run, without dropping tests or relaxing product CPU/request
deadlines. Development overlays require an explicit `--allow-dirty` flag and
cannot stand in for clean release evidence. CI owns no SSH
credentials, deployment authority, or product CD. Product containers never
receive a Docker socket or host service control.

The shard runner retains case IDs for coverage, then releases its discovery
and selection lists before executing. Unittest can therefore reclaim completed
fixture instances; complete discovery must not retain every fixture's data
until the shard exits. An executable collection regression protects this
memory boundary.

The required `test-compose` check depends on successful source CI and runs the
actual `scripts/test-python-final.sh` suite on a fresh GitHub-hosted Linux Docker
runner, with a 360-minute limit. It runs for `dev` pushes, release PRs and
manual workflow dispatches. Both test jobs check out the exact PR head SHA or
event SHA with persisted Git credentials disabled. Test-only pinned browser
dependencies, NSS tools and the runner's verified Chrome support the fixtures;
no deployment credentials or production machine is involved.

The `promote-main` guard proves a unique merged same-repository `dev -> main` PR,
successful exact-source `dev` push CI including both `source` and `test-compose`,
and identical reviewed/main trees. A `main` push reuses that proven tree rather
than repeating source or native builds. Missing, skipped or failed required jobs
cannot satisfy promotion. A newer
commit invalidates earlier CI and native acceptance evidence.

## Complete native acceptance

Run `scripts/test-python-final.sh` from a clean exact source checkout on an
isolated Linux Docker host. `FC_EVIDENCE_DIR` is an absolute private evidence
folder outside the checkout; `FC_BROWSER_PYTHON` selects an installed test-only
Python environment containing pinned httpx/Playwright and Chromium.

The suite builds actual product Dockerfiles, runs the same complete unit
discovery in four isolated containers, each limited to one CPU/512 MiB, verifies
their receipts against fresh discovery, checks source and browser JavaScript, then
runs real ACME/Nginx and native Direct/Relay TLS fixtures and the three-node
Chromium federation harness. The same gate runs real optional-MySQL migrations,
cold database/control backup and new-project restoration, and the actual offline
identity-preserving endpoint wrapper for both databases.

`scripts/test-python-business-traffic.sh` also verifies actual rate-limited
Nginx sendfile and upload observations before transfer completion, TCP refusal
of the private counter handler, and real-time quiet recovery. Nginx resources
are sampled during the fixture. Python fixture CPU/RSS includes its HTTP test
client and receiver, so it is not a standalone observer-overhead measurement.

The native runner also invokes `tests.resource_inventory_native` in an isolated
single-core container without published host ports. It uses the actual HTTPS
control transport, durable HMAC nonces and node-signed inventory pages with a
temporary private CA. Both wrong-root and hostname failures must be observed;
certificate verification cannot be disabled. The case observes 1,074 entries
without changing source bytes. It also verifies registered plaintext/GCM media
and a recording with an opaque encrypted-lyric footer through the actual
content-observation client. Media key operations are forbidden by active test
guards. These metadata/content proofs do not establish admission, migration or
authority cutover.

`tests.resource_planning_native` additionally runs two actual HTTPS peer apps
and a Master planner against three separate SQLite stores in one isolated
one-CPU/512-MiB fixture. It waits for the real startup quiet interval, verifies
plain/GCM source content without deriving media keys, repeats native inventories,
accepts the readonly plan, then refuses a newly occupied target parent. The
fixture's temporary filesystem must exceed the unchanged 1 GiB physical reserve.
Its input and output are part of the complete native runner. It does not prove
separate-host transfer, admission, publication, migration cleanup or retirement.

Fixtures use a private local ACME CA, actual HTTP-01/JWS/CSR/account operations
and test-only trust roots. They do not contact a public CA, publish host HTTP/HTTPS
ports, disable certificate checking, or affect existing deployments. Test-only
CA images must never become product deployment images.

CI uploads only explicitly selected sanitized JSON receipts, the source commit
receipt and test logs. Private CA directories, NSS profiles, database/control
snapshots, certificate pairs and runtime keys are excluded. The artifact collector
rejects private-key PEM material; it never uploads the fixture or deployment
directories. These workflow artifacts expire after 14 days and are not a permanent
evidence archive. A durable summary belongs in the existing acceptance ledger.
Independent exact-source native evidence remains required for the
final archival deliverable.

The S3 candidate adds an actual-MySQL transaction suite through
`tests.s3_uploads_mysql_native`, using the same publication, lost-acknowledgement,
cancellation, recovery-credential, live-claim and expiry scenarios as SQLite.
`tests.s3_uploads_native` separately uses a pinned compatible service over ordinary
verified TLS, independent direct HTTP payloads and a second Python process for
recovery. It proves service transactions, not Admin HTTP or browser completion.
Its test-only bucket, credentials, database and CA must remain outside public
artifacts. Packaging its reproducible native fixture and completing actual GUI
acceptance remain gates in the current renewal ledger.

Every Web fixture has one ASGI worker and one CPU. A timeout is a failure; stop
fixtures before cleaning them. After business work, require CPU below 20% for
five consecutive one-second samples within 30 seconds. STUN probes are serialized.

Source policy and each unit shard have a 60-minute budget; source aggregation
has 15 minutes, and the complete Compose job has 360 minutes. The ordinary gate
contains four actual one-hour profiles: mixed
partial cancellation on SQLite and MySQL, successful full-task cleanup over
native TLS, and complete GUI migration followed by independent retirement with
a held Nginx Relay reader. Independent waits overlap; their count is not a
serial wall-time estimate. The 2026-10-20 single-core source sweep took 2,166.579 seconds for
1,297 cases (ten dedicated-environment skips), exceeding the former 30-minute
job budget. These job budgets include serial fixture setup, builds and repeated
regression; they do not change any product CPU limit, per-fixture timeout or
quiescence requirement. Complete native acceptance still repeats unit discovery;
no case is removed to fit a shorter workflow. Additional standalone migration
profiles remain available for diagnosis and retain their own exact-source evidence.

Independent actual-hour profiles overlap unrelated fixture work. SQLite mixed
cancellation and successful full-task cleanup run in separately named owned
background containers; the GUI hour runs as a tracked process. All three must
join successfully before the operator runner starts. The operator runner finishes its MySQL suite before host-side
operator fixtures: concurrent container creation can prevent a stable global
mount-owner census, which correctly refuses cold payload movement. Every
background phase must be joined successfully
before the source receipt or final success is written. Cleanup signals only
jobs still tracked by that shell and removes only its named containers before
joining. `tests.native_runner_cleanup` exercises the actual cleanup functions
with disposable Docker controls, a path containing spaces and an unrelated
sentinel. Its two passing cleanup cases are not full-workflow acceptance.

The Master migration preparation candidate adds a verified-TLS, actual
child-process recovery check in `tests.resource_admission_native` and a real
MySQL transaction suite in `tests.resource_admission_mysql_native`. Public native
receipts contain only sanitized metadata, never the private resume database,
vault-key fixture or CA files. These prove preparation and controlled unstarted
cancellation; direct media transfer and publication require separate gates.

The direct-copy candidate supplies its separate
`tests.resource_transfer_native` gate: actual certificate-verified Source/Target
bytes, a partial failed receive, an independent Target process, original and
ciphertext hashes, one-core/512-MiB constraints and whole-cgroup quiescence.
With `FRONTIERCLOUD_CANCEL_NATIVE=1`, it also exercises higher-epoch peer stops,
real disconnect read-lease/connection cleanup, exact signed error verification,
unknown-stage refusal, retained capacity and known-file-only cancellation.
`tests.resource_transfer_mysql_native` checks actual MySQL journals and bindings.
`tests.resource_cancel_mysql_native` separately checks cancellation and failed
settlement against separate disposable MySQL node databases.
Their in-process ASGI bytes are not relabelled as TLS proof. Public receipt names
are `native-transfer.json` and `native-transfer.log`. Private child config,
staging files, vault keys and CA material remain excluded from artifacts.
These gates do not establish whole-parent publication, source cleanup or retirement.

`tests.resource_finalization_mysql_native` separately checks successful task-wide
census/release and fault recovery against three actual MySQL databases per case.
Its grace clock is explicitly virtual. The default final gate additionally runs
the complete `FRONTIERCLOUD_CLEANUP_NATIVE=1` profile of
`tests.resource_transfer_native` in an owned one-CPU/512-MiB background container,
with its existing 4,100-second timeout and original assertions. It waits the
unchanged hour, keeps a real Source HTTP reader open beyond capability expiry,
refreshes authenticated heartbeats and verifies every Source replica's removal,
all Target plaintext/ciphertext bytes, exact three-party fence release and both
native completion receipts. Wrong CA and hostname tests remain enabled. Its
sanitized `migration-hour.json`, `migration-hour.log` and
`migration-hour-source-commit.txt` join the ordinary CI artifacts. Neither virtual
unit clocks nor the mixed-cancellation or GUI profiles substitute for these
successful-completion assertions. `scripts/test-python-migration-hour.sh` retains
the same environment and assertions as an independent diagnostic entry, using
the native test-tools image, a clean checkout and external evidence. A successful
default final gate includes this profile; a separate repeat is not required.

`tests.resource_cancel_cleanup_mysql_native` is part of the ordinary MySQL
runner. It checks mixed committed/cancelled cleanup with virtual time, including
retained Source files, readers, damaged Targets and lost-reply recovery. The
separate `FRONTIERCLOUD_CANCEL_MIXED_NATIVE=1` TLS fixture and its guarded
`tests.resource_cancel_mixed_https_mysql_native` wrapper wait the actual original
hour; their earlier generation-8 profiles pass 57 checks per database. They need
their own final-source acceptance and do not establish
whole-task cancellation, usage reconciliation or source-drain release.

The mandatory coordinator TLS profile now also enables
`FRONTIERCLOUD_CANCEL_CENSUS_NATIVE=1`; its 76 checks include the earlier 65
coordinator checks and keep their existing artifact names. The guarded MySQL
counterpart is `tests.resource_cancel_census_https_mysql_native`. Its ordinary
MySQL gate additionally runs `tests.resource_cancel_census_mysql_native` for
native current-resource census and generation-9 DDL/lock recovery. These are
Source observations, not Master catalog agreement or terminal cancellation.

`tests.compose_layout_native` renders the actual default, explicit MySQL and
Storage Compose inputs. It checks that SQLite does not activate MySQL while the
documented overlay does, preserving MySQL volumes and secret mounts.
`tests.nodes_browser_native` verifies collapsed/lazy cards, keyboard interaction,
refresh state, local certificate placement and hashed CSS through actual Nginx
HTTPS with disposable browser trust. Its keys, profile and screenshot stay private;
only `nodes-browser.json` and the sanitized log are CI artifacts. Native fleet
project names hash the entire fixture path, avoiding old suffix-name volume reuse.

`tests.player_storage_browser` uploads separate plain/encrypted MP3 albums
through the actual Admin chooser for Local, Direct and Relay. Each includes an
MP3 above 128 MiB and a distinct short following track. It checks bounded MSE
startup, manual switching, a seek to the real tail followed by automatic
switching, and each track's original embedded image bytes and display dimensions.
It does not claim elapsed playback of the whole long track. Browser ciphertext
decryption remains in the ordinary worker; Blob images use the page's allowed
image surface rather than weakening its connection CSP. Inputs, pipeline
snapshots, enforced Web limits and CPU recovery belong to
`player-storage-browser.json`; private uploaded media, keys and browser profiles
are excluded from CI artifacts. The candidate component passes 46 checks across
all six albums, with no Web OOM. Master reaches its 512 MiB cgroup limit with
240 pressure events; an empty paired-fixture comparison attributes most final
growth to file cache, with about 4–6 MiB higher final process RSS. This is not
peak process RSS or absence-of-pressure proof. Final-source verification and
the remaining cache/interruption/lyric browser matrix remain required.

## Evidence and archival

The upload transport smoke exercises native-XHR abort/offline settlement,
inactivity rather than total-duration limits, late-event cleanup, Direct
credential isolation and bounded cancellation/status reads. The existing
`ADMIN_UPLOAD_INACTIVITY_TIMEOUT` is exposed by authenticated status and drives
both browser upload watchdogs and server recovery; its default remains 300
seconds. A continuous transfer can exceed that duration. The separate
`FC_FILESYSTEM_UPLOAD_FAULTS=1` browser profile uses actual throttled partial
Local/Direct/Relay uploads, network loss/refresh and unchanged worker deadlines.
Its source data, private certificates and databases are not CI artifacts.
The five-node `FC_FIVE_NODE_TRAFFIC=1` profile reuses the capped fleet setup,
checks real business-byte pauses/manual persistence and stops at cleanup grace.
Neither profile substitutes for the separate actual-hour terminal gate.

The native per-unit cancellation profile runs Target shadow rollback, Source
retention and unit settlement in sequence, preserving their earlier assertions
and process-recovery cases. The default SQLite/MySQL gates run this expanded
profile once. Standalone Target/Source profiles remain available for focused
diagnosis without repeating their shared native setup in every final gate.
The separate Master-coordinator profile exercises persisted stops, current
Source proofs and lost native responses over verified TLS. Its actual inputs
and receipts are distinct from the lower-level per-unit profile. Neither
profile proves whole-task cancellation or replaces its remaining integration.

Keep sanitized results and exact source identity with the CI run. All
required gates in [the current renewal ledger](../python-current-parity.md) must
pass before archival, including the owner-authorized S3 release criterion
recorded in the ledger.
The [prior acceptance ledger](../python-final-parity.md) is historical evidence;
it must not be relabelled as proof for the current source. A passing narrow unit
test is never sufficient archival evidence. Retain Python `main` and `dev`;
do not create implementation branches or add Gin recovery refs. GitHub's native archive
operation makes the repository read-only and can be reversed in Settings.
[Recovery](../operator-recovery.md) explains private deployment backup and restore.
