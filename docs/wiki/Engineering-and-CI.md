# Engineering and CI

## 1. Branch policy

The normal repository workflow is:

```text
dev
 |
 v
push CI
 |
 v
dev -> main PR
 |
 v
manual merge
 |
 v
main
```

Do not create additional feature/fix branches by default. If a new branch is genuinely required, obtain explicit maintainer approval first.

Do not push ordinary engineering changes directly to `main`.

## 2. Technology stack

Major technologies:

- Python / FastAPI;
- SQLAlchemy async;
- MySQL;
- Redis;
- Nginx;
- Docker Compose;
- native HTML/CSS/JavaScript;
- Rust/WASM karaoke module;
- GitHub Actions.

## 3. Repository layout

```text
app/
  api/                 API and Admin endpoints
  core/                configuration, database, schema migrations
  services/            business services, federation, release, observability

static/
  js/                  Admin, media browser, player, and browser logic

nginx/                  Nginx image/configuration
updater/                Updater control component
scripts/                CI and source-policy scripts
tests/                  unit, runtime, browser, cluster, regression tests

docker-compose.yaml     default deployment topology
Dockerfile              Web image
README.md               quick entry point
docs/wiki/              detailed project Wiki
```

## 4. Local baseline checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

```bash
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
```

```bash
docker compose config --quiet
```

GitHub Actions remains the authoritative final validation for a pushed head.

## 5. CI topology

Main workflow:

```text
.github/workflows/docker.yml
```

Primary gates:

```text
verify-promotion-query
browser-ui
test-cluster
        |
        +------+
               v
          test-compose
```

`test-compose` is the aggregate gate and verifies its upstream gates before running the rest of the stack tests.

### verify-promotion-query

Validates exact dev SHA workflow lookup semantics used by release verification.

### browser-ui

Starts a real application stack and runs Chromium UI regression tests. This validates browser behavior rather than merely checking HTML strings.

### test-cluster

Builds a real multi-node HTTPS topology and validates role fixing, pairing, heartbeats, resource configuration, Direct/Relay behavior, cluster control, and Follower behavior.

### test-compose

Runs/aggregates:

- source configuration tests;
- frontend syntax;
- Compose build/start;
- unit/runtime tests;
- edge/security tests;
- public/admin flows;
- cleanup.

## 6. Exact-SHA rule

A green result for an older commit never proves a newer head is valid.

Incorrect:

```text
SHA A is green
new commit creates SHA B
reuse A's green result because the diff is small
```

Correct:

```text
final SHA B
   |
   v
B runs its own full CI
   |
   v
merge decision uses B's result
```

The release verifier follows the same principle.

## 7. Source contracts vs runtime tests

Some requirements are source/deployment contracts, for example:

- runtime image tags remain patch-pinned;
- a sensitive mount remains read-only;
- release UI stays in its owning module;
- security configuration does not regress.

These belong in source-policy/configuration checks. Do not copy Dockerfiles or Compose files into the business image merely so a runtime test can read them.

Tests must follow module ownership. If UI responsibility moved from one JavaScript file to another, update the test contract instead of re-inserting unrelated text into the old module.

## 8. Database change rules

Every schema change must:

1. update the latest empty-database bootstrap schema;
2. add an explicit new Generation migration;
3. make that migration resumable/idempotent;
4. advance the generation only after success;
5. add migration regression coverage;
6. add a real MySQL upgrade smoke when practical.

Never return to the old policy:

```text
schema changed -> existing database must be recreated empty
```

## 9. Cluster model changes

When changing Storage/Compute/Backup behavior, verify:

- Desired and Observed remain distinct;
- an offline Follower cannot be shown as effective;
- heartbeats do not overwrite durable execution facts;
- the UI clearly distinguishes configuration from runtime state;
- upgrade compatibility for existing nodes is preserved.

For example, Backup `last_success` is a durable result and must not be erased by repeated configuration synchronization.

## 10. Admin UI standards

The Admin UI should not be a raw database-field dump.

For an operational state it should answer:

```text
What is happening now?
What is desired?
Did the change actually become effective?
When was the most recent success/failure?
Why was this scheduling/placement decision made?
Where should the operator look next?
```

For example, Compute should not stop at:

```text
Slots 4
```

It should expose values such as:

```text
running 2 / 4
queued 7
24h completed count
failures/retries
CPU / memory
placement reason for recent jobs
```

## 11. Release-control changes

Release control is high risk. Changes in this area require regression coverage for relevant behavior including:

- GitHub PR provenance;
- exact dev CI lookup;
- tree equality;
- rate limiting/backoff;
- last-known-good display semantics;
- `can_upgrade` authorization;
- rollback target;
- Follower convergence;
- maintenance behavior.

External verification data may be cached for observability, but stale data must not authorize a new dangerous operation.

## 12. Never break the runtime design to satisfy a test

Examples of bad fixes:

- copying deployment source files into the business image because a test cannot see them;
- putting obsolete UI text back into the wrong module because a source test expects it;
- removing real MySQL migration smoke coverage because it reveals an event-loop/pool problem;
- weakening fail-closed release rules to make a regression test pass.

Fix the responsibility boundary or the real implementation instead.

## 13. Pull request content

A useful PR description should identify:

- the problem;
- root cause;
- affected modules/files;
- safety boundary;
- database/upgrade impact;
- regression coverage;
- final exact head SHA;
- CI run number/result.

Avoid descriptions that only say `fix bug`.

## 14. Merge is not deployment

A PR merged to `main` still has to pass release verification and be explicitly deployed.

```text
PR merged to main
       |
       v
release verifier checks provenance + dev CI + tree
       |
       v
Admin starts upgrade
       |
       v
Master/Followers build and replace
       |
       v
cluster convergence
       |
       v
complete
```

Engineering complete, code merged, and the running cluster upgraded are three different states.
