# Engineering and CI

## 1. Repository topology

The repository has two independent authorized runtime profiles:

```text
gin_dev / gin_main  native implementation / reviewed release
dev / main          Python reference implementation / reviewed release
```

Absolute policy:

- changes go to the selected profile's implementation branch;
- do not create feature/fix/release/temporary/conflict-resolution branches;
- only same-repository `dev -> main` and `gin_dev -> gin_main` PRs are valid;
- never force-push or rewrite canonical refs;
- after promotion, synchronize its implementation branch with the release merge.

Repository Policy workflow detects non-canonical branch creation events and invalid PR topology. True pre-creation branch prevention depends on GitHub repository/ruleset administration, so engineers must still follow the documented invariant.

## 2. Read architecture boundaries first

Before changing cross-cutting behavior, read:

```text
ARCHITECTURE.md
CONTRIBUTING.md
```

The Wiki explains current behavior, but those repository files define non-negotiable constraints that must survive feature work.

Important examples:

- one business Master;
- Followers are resource nodes;
- Compute Worker is retired;
- one complete media object has one storage owner;
- folder rename is same-parent only;
- media mutations share one fence model;
- Web remains one native process or one Python ASGI worker;
- lyrics support at most two directory levels below `lyrics`;
- `lyrics/default.lrc` is internal fallback content;
- Admin module order is contractual;
- releases use exact runtime-specific promotion and CI provenance.

## 3. Technology stack

Major technologies:

- Go / Gin (default native runtime and updater);
- Python / FastAPI / SQLAlchemy async (explicit reference profile);
- SQLite (default) or MySQL with the same logical schema;
- Redis;
- Nginx;
- Docker Compose;
- native HTML/CSS/JavaScript;
- Rust/WASM karaoke/audio support;
- GitHub Actions.

## 4. Repository layout

```text
app/
  api/                 reference API, Admin, internal control endpoints
  core/                settings, database, schema generation/migrations
  services/            business services, federation, storage, release, observability

cmd/                    native Web, initializer, maintenance and updater commands
internal/               native domain/repository/protocol implementations
protocol/               shared cross-language contracts and vectors
migrations/             shared logical schema and generation migrations

static/
  media/               public/Admin/player HTML
  js/                  browser/Admin/player modules
  css/                 UI styles

nginx/                  Nginx image/configuration
updater/                release Updater component
scripts/                CI/source-policy helpers
tests/                  unit/runtime/browser/cluster/regression coverage

.github/workflows/       CI and repository policy
docker-compose.yaml      supported deployment topology
ARCHITECTURE.md          architecture invariants
CONTRIBUTING.md          repository/Git collaboration rules
README.md                current quick project overview
```

`docs/wiki/` contains reviewable documentation sources. The published GitHub
Wiki is a separate repository; changing these files does not publish the Wiki.

## 5. Local baseline checks

Before pushing a meaningful change:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
go test ./...
go vet ./...
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
docker compose config --quiet
```

Exact final source push CI for the selected profile remains release authority.
For disposable Linux hosts only, native integration runs:

```bash
bash scripts/test-go-business.sh
bash scripts/test-go-deployment.sh
FRONTIERCLOUD_REVISION="$(git rev-parse HEAD)" bash scripts/test-native-default.sh
bash scripts/test-go-updater.sh
bash scripts/test-mixed-runtime.sh
```

These scripts create private fixtures, not permission to redeploy existing nodes.

## 6. CI topology

All hosted CI jobs have an explicit maximum of **three minutes**. Test jobs
have a 150-second inner deadline; promotion/policy jobs stay at one or two
minutes. Jobs run independently, without serial build/test chains.

The source gate in `.github/workflows/docker.yml` checks repository/release
policy, the one-core boundary, syntax and lightweight frontend contracts.
`runtime-conformance.yml` validates protocol JSON and canonical schema assets.
`scripts/check_ci_budget.py` rejects missing/extended/dynamic job timeouts,
heavy integration commands and serial job chains. Regression tests cover it.

Hosted CI does **not** build runtime images, launch MySQL/Redis/browser fixtures,
or run private-CA ten-node mixed fleets, upgrades, rollbacks or race suites.
Those checks execute directly on the operator-provided development host,
**not** through a self-hosted Actions runner or a detached CI background job:

```bash
bash scripts/test-store-interop.sh
bash scripts/test-go-business.sh
bash scripts/test-go-deployment.sh
FRONTIERCLOUD_REVISION="$(git rev-parse HEAD)" bash scripts/test-native-default.sh
bash scripts/test-go-updater.sh
bash scripts/test-mixed-runtime.sh
bash scripts/test-mixed-release.sh
```

Use disposable, egress-isolated fixtures. Preserve the exact immutable source
SHA, image digests, command logs, start/end times and actual results on that
host. Existing production/development nodes are not disposable test fixtures.
A fast green hosted CI run is **not** proof that these acceptance checks passed
and is never sufficient by itself to authorize a production migration or merge.
Reviewed source provenance, matching development-host acceptance evidence,
backup/recovery proof and explicit operator approval remain release conditions.

The previous native jobs with 65/120-minute limits and the 25-minute real-store
job violated the operator's CI budget. They are removed, not shortened while
silently retaining workloads that cannot fit. Original main/dev branch changes
require the separately approved branch promotion; no merge is implicit here.

### One-core compute boundary

Run `scripts/check_cpu_boundary.py` in fast CI. Exercise runtime CPU recovery on
the development host with bounded fixtures and `scripts/check_cpu_quiescence.py`.
Linux load average, CPU utilization, memory PSI and swap usage are separate
signals; a runtime-language change alone does not demonstrate pressure relief.

## 7. Exact-SHA rule

Do not reuse an older green run after changing either implementation branch.

```text
final dev SHA
   |
   v
its own push CI
   |
   v
merge/release decision
```

A one-line documentation or code fix creates a new SHA and therefore new release evidence.

## 8. Source contracts vs runtime behavior

Some requirements are best enforced from source/deployment configuration, for example:

- runtime image/tag pinning;
- read-only or restricted mounts;
- no tracked runtime data under `data/`;
- one ASGI worker deployment invariant;
- repository topology;
- module ownership;
- security proxy configuration.

Do not distort the runtime design merely so a test can read the wrong file or find a historical string.

If responsibility moves from one module to another, update the regression to follow the new owner rather than re-inserting obsolete code/text.

## 9. Managed-media mutation rules

Path/metadata operations are high-risk because filesystem bytes and selected
SQLite/MySQL facts form one business lifecycle.

Current mutation model:

- cross-member folder rename owns the exclusive process-local fence;
- upload reservation, delete, hide/unhide, and priority mutations join the shared boundary;
- durable upload leases and pending-delete state continue as database fences after the short shared lock;
- failures after partial cross-member rename roll back moved members in reverse order.

When adding a new path mutation, join this protocol. Do not create an independent lock/race window.

### Single-worker requirement

Python's mutation fence is process-local. Native filesystem mutation retains OS
leases, but production Web remains one process in either profile.

Do not add:

```text
WEB_CONCURRENCY > 1
multiple Gunicorn/Uvicorn workers
horizontal Web replicas sharing the same business state
```

unless the mutation fence is first replaced with a database/distributed lock and cross-process concurrency coverage is added.

## 10. Folder rename rules

Rename is not a general move API.

It must remain:

- `music` / `vido` managed folders only;
- same-parent only;
- collision-safe;
- upload/pending-delete aware;
- cluster-aware;
- rollback-capable;
- metadata-consistent.

A deleted target name can be reused when only stale directory metadata remains, but real active/path collisions still block.

Do not casually expand rename into cross-parent move semantics; that requires a new transaction design and federation regressions.

## 11. Storage placement rules

External Admin upload APIs choose a site type, not a concrete Follower:

```text
primary
direct
relay
```

For Direct/Relay, placement must choose from current ready members of that type. Member selection and durable reservation stay inside the storage write lock.

Historical media ownership is not rewritten merely because UI/site-type presentation changes.

Capacity logic must distinguish:

```text
physical capacity
logical allocation
used bytes
reserved bytes
writable/health
```

Do not use a single percentage to represent all of them.

## 12. Compute retirement rule

Compute Worker is retired.

Do not reintroduce:

- Compute cards/settings in Admin;
- worker slots as active configuration;
- job leasing/scheduling loops;
- product-level worker observability;
- task APIs that make a Follower an active Compute Worker.

Historical tables/data can remain for upgrade/backup compatibility until an explicit destructive migration removes them.

## 13. Lyrics rules

The supported hierarchy is bounded:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Every surface must enforce the same maximum depth.

`lyrics/default.lrc` is internal runtime fallback content and must stay excluded from user trees, search, counts, and business relation counts.

Same-name auto-link is fallback automation. It may fill missing/default state but must never overwrite an explicit user relation.

Accepted lyric upload is durable only after both the file and managed-object registration succeed together. Do not create file-only half-state.

## 14. Catalog/cache rules

Redis generation catalogs avoid repeated expensive scans/sorts. Successful mutations that change catalog membership/order/visibility/path must invalidate the relevant generation.

HTTP/browser caching must not make a successful mutable operation look stale for long.

Cache invalidation after a durable business commit is fail-soft. A Redis error must not roll back already-committed business facts merely to make the client see an error.

## 15. Heartbeat and Backup rules

Heartbeat and Backup are independent failure domains.

Do not:

- run Backup inline in a way that blocks heartbeat;
- mark a healthy heartbeat failed because Backup later failed;
- describe Backup as live replication or automatic failover.

Backup artifacts are bounded recovery packages. Large transfers are chunked and should clean/abort interrupted receiving generations when supported.

## 16. Database change rules

Every schema change must:

1. update the latest empty-database bootstrap schema;
2. add an explicit next Schema Generation migration;
3. make the migration idempotent/resumable;
4. advance generation only after success;
5. add migration regression coverage;
6. add real MySQL upgrade smoke coverage when practical.

Never restore the old policy:

```text
schema changed -> destroy/recreate existing database
```

## 17. Admin UI contract

The final module order is intentional:

1. Media management
2. Media sorting policy
3. Lyric relations
4. Karaoke users
5. IP security
6. WebRTC network relations
7. Nodes
8. Site access state
9. System release management
10. Admin Key
11. Brand Logo

Dynamic modules must participate in the same visual and DOM order. Reorder logic must be idempotent; DOM observers must not create self-triggering move loops.

Operational UI should explain semantics rather than dump raw fields. Examples:

- physical used/total vs FrontierCloud used/allocated;
- Desired vs Observed;
- current release vs pending target;
- direct hidden vs inherited hidden;
- site type vs actual storage member.

## 18. Release-control change rules

Release control is high risk. Relevant changes need regressions for:

- merged runtime-specific promotion provenance;
- exact newest source push CI lookup;
- reviewed source/release tree equality;
- GitHub rate limiting/backoff;
- last-known-good display-only behavior;
- `can_upgrade` authorization;
- rollback target;
- cluster convergence;
- maintenance safety;
- release UI semantics.

External/stale verification data may aid observability but must never authorize a new dangerous operation.

## 19. Pull request content

A useful runtime-specific promotion PR should state:

- problem/root cause or feature intent;
- affected behavior/modules;
- architecture/safety boundary;
- migration/upgrade impact;
- regression coverage;
- final exact source SHA;
- exact CI run/result.

Avoid vague descriptions such as only `fix bug`.

## 20. After merge

After the PR is merged:

1. record the resulting profile's release merge commit;
2. fast-forward its implementation branch to that commit without force;
3. only then start the next development cycle.

This keeps the two-branch history convergent and preserves the release verifier's provenance assumptions.

## 21. Wiki publishing

The GitHub Wiki is an independent Git repository. When the Wiki needs a bulk refresh, use a temporary, reviewable one-shot publisher from `dev`, verify the Wiki push succeeded, then remove the temporary publisher/source so the main code repository does not pretend that an in-repo Wiki tree is the live documentation source.
