# Contributing to FrontierCloud

FrontierCloud uses two authorized two-branch provenance pairs and one deployable runtime (Gin/Go). These rules are architectural constraints, not suggestions.

## Repository topology — MUST NOT violate

- `gin_dev` is the **only development branch** for the current reconstruction; `dev` remains the canonical development branch pending separately authorized consolidation. Neither pair permits Python deployment.
- `main` and `gin_main` are their respective release branches. Only same-repository `dev -> main` and `gin_dev -> gin_main` promotions are valid.
- **Do not create any new branch** outside these owner-authorized refs. This includes `feature/*`, `fix/*`, temporary conflict-resolution branches, experiment branches, or automation-created branches.
- Do not open a feature/fix branch directly against `main`.
- Do not force-push or rewrite canonical branches.
- Do not commit implementation work directly to `main` or `gin_main`.
- After a release PR is merged, fast-forward its development branch to the resulting release merge commit before further implementation.
- Historical non-canonical branches may exist until the repository owner deletes them. They are not implementation targets and must not be reused.

The repository can fail an invalid PR topology, but repository-local code cannot reliably prevent somebody with GitHub ref permission from creating a branch. The **no-new-branch rule therefore remains an explicit human/automation invariant** and should also be mirrored in the GitHub Wiki and repository ruleset/branch-protection settings.

## Change discipline

1. Read `ARCHITECTURE.md` before changing a cross-cutting subsystem.
2. Start from the selected profile's development HEAD. Synchronize it with a just-promoted release first as described above.
3. Change the smallest coherent surface. Do not revive retired compatibility/product features to make a test pass.
4. Add or strengthen regression coverage for every bug fix and every new invariant.
5. Run the bounded source CI checks and native acceptance on the development host. A narrow unit test is not release evidence; heavyweight acceptance must never be added to hosted CI.
6. Open a release PR only when authorized, using the profile's fixed promotion pair. The repository owner performs the merge.

## Documentation ownership

Keep top-level documentation intentionally separated:

- `README.md` is the concise product entry point, shortest startup path, and documentation map.
- `ARCHITECTURE.md` owns cross-cutting invariants and prohibited design drift.
- `CONTRIBUTING.md` owns repository, testing, review, and release procedure.
- The separate [GitHub Wiki](https://github.com/wongyiuming/FrontierCloud/wiki) owns operator/subsystem guidance; docs/wiki must not be Git-tracked. docs/ holds focused engineering contracts and audit evidence.

Do not copy a full architecture or operations manual back into the README. When behavior changes, update the narrowest authoritative document and link to it from the entry point only when discovery would otherwise suffer.

## Regression policy

Prefer executable protection over prose:

- Business invariants belong in unit/runtime tests.
- Browser behavior and Admin layout belong in Chromium acceptance tests when practical.
- Master/Follower behavior belongs in federation tests when it crosses node boundaries.
- Nginx/Compose/release assumptions belong in source/deployment contract tests.
- A rule that cannot be reliably asserted from the repository (for example, **never create a new Git branch**) must be documented here and in the Wiki instead of being implied by convention.

Never weaken an existing regression merely to make a new implementation pass. If an invariant genuinely changes, update the architecture documentation and the test in the same change and explain why.

## Non-negotiable architecture boundaries

- A cluster has one business Master. Followers are resource nodes, not independent business authorities.
- Master-owned business state (lyrics, lyric relations, playback/business facts, users, audit facts) must not silently migrate to Followers.
- One managed media object is complete and belongs to exactly one storage member. Do not split one logical object across storage members.
- Compute Worker is retired. Do not add worker slots, compute scheduling, worker UI, or worker product configuration back without an explicit architecture decision.
- Product runtime changes that add burst or sustained high-load computation are rejected. Do not add transcoding, compression, inference, bulk transformation, process execution, executor offload, or compute-heavy runtime dependencies. This is a one-core service boundary, not a tunable CPU budget.
- Development-host acceptance runs the Web container with one CPU. A compute-caused test timeout fails the build; failure handling stops the Web fixture before cleanup. After business tests, CPU must fall below 20% of one core for five consecutive one-second samples within 30 seconds. The threshold detects failure to recover and does not authorize compute-heavy work below it.
- Heartbeat health is independent from Backup work. Backup failure must never mark a healthy node offline.
- Backup is a recovery artifact, not online replication, HA, or automatic failover.
- Managed paths are changed only through FrontierCloud transactions. Do not rename/move/delete managed files directly on disk as a substitute for metadata updates.
- Folder rename is a rename inside the same parent, not a general move API. Cross-member rename must remain rollback-capable. Master rename is the exclusive path mutation; upload reservation, delete, hide/unhide, and priority changes must participate in the same mutation-fence protocol.
- Lyrics support at most two directory levels below `lyrics`: `lyrics/<category>/<subdir>/<file>.lrc`. Every upload, catalog, tree, search, download, and delete surface must enforce the same boundary.
- `lyrics/default.lrc` is an internal playback fallback. It is not user content and must not appear in Admin lists/counts/search or be exposed as a normal mutable object.
- Same-name lyric auto-link is manual fallback automation: only the explicit Admin control may trigger it. Upload/refresh paths must not invoke it; it may replace missing/default fallback state but **must never overwrite an explicit user-managed lyric relation**, even if that selected lyric file is temporarily unavailable.
- Admin module order is intentional and protected by regression tests. Do not reorder modules incidentally while changing a module.

## Validation workflow

Hosted CI is restricted to lightweight source/policy/JavaScript smoke checks and three-minute parallel jobs; scripts/check_ci_budget.py rejects heavyweight acceptance and serial job chains. Do not raise this budget or use background jobs to evade it.

Run native unit/race, actual SQLite/MySQL business/API, deployment and updater tests on the development host. Python remains a test/script language, but application tests target Gin HTTP/native source; importing app, main, FastAPI or SQLAlchemy in tests is prohibited. Reference app/ and updater/server.py are illustrative only and must not be deployed. Keep syntax examples aligned when relevant; runnable Python compatibility is not a product requirement.

The fleet is exactly five Go nodes: one Master, two Direct, two Relay. scripts/test-native-matrix.sh covers both database combinations; scripts/test-native-release.sh exercises the same native fleet's upgrade/rollback. These and real browser tests are development-host-only. Legacy data/token golden vectors are retained independently of Python runtime support.

## Release evidence

A release is valid only when the exact development commit has successful CI and the production tree is identical to the reviewed source tree. Both `dev -> main` and `gin_dev -> gin_main` use this rule. Post-merge proof is an additional guard, not a replacement for review. A local tested commit is not an already-published production release.

The complete local baseline and CI composition are maintained in [`docs/wiki/Engineering-and-CI.md`](https://github.com/wongyiuming/FrontierCloud/wiki/Engineering-and-CI). Deployment, rollback, and migration procedures are maintained in [`docs/wiki/Release-and-Database-Migrations.md`](https://github.com/wongyiuming/FrontierCloud/wiki/Release-and-Database-Migrations).
