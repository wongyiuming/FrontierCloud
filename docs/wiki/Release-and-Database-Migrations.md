# Release, Rollback, and Database Migrations

## 1. Canonical branch topology

FrontierCloud has exactly two canonical branches:

```text
dev   implementation + complete CI authority
main  reviewed release history
```

Repository policy is strict:

- do not create feature/fix/release/temporary branches;
- all engineering changes go directly to the existing `dev`;
- the only valid PR into `main` is same-repository `dev -> main`;
- never force-rewrite `dev` or `main`;
- after a release PR is merged, fast-forward `dev` to the resulting `main` merge commit before further work.

The no-new-branch rule is an architecture constraint, not a preference.

## 2. Normal release path

```text
dev change
   |
   v
exact dev push CI
   |
   v
same-repository dev -> main PR
   |
   v
manual review / merge
   |
   v
main release history
   |
   v
runtime release verifier
   |
   v
Admin starts upgrade
   |
   v
Master build/replace
   |
   v
Follower distribution
   |
   v
cluster convergence
```

Engineering complete, merged to `main`, and deployed/converged are three different states.

Automation must enter this same flow through `POST /api/v1/media/admin/nodes/release/upgrade` (the Admin “upgrade and distribute” action). It must use verified HTTPS plus an Admin session and CSRF token, then wait until the Master and every active Follower report both the target application SHA and target Updater runtime SHA in `success`. Direct Git resets or Compose replacement by external CD bypass the release policy, maintenance gate and cluster convergence protocol.

The retained development environment uses a root-owned reconciliation timer around this API. The timer is only an operator client; the Updater remains the component that fetches, builds, replaces, distributes and records rollback state.

## 3. Why `main` HEAD alone is not enough

A commit being present on `main` is not sufficient proof that it followed the accepted development path.

The release verifier requires:

1. current `main` HEAD is associated with the accepted merged same-repository `dev -> main` PR;
2. the promoted source `dev` SHA has an exact `push` workflow run;
3. that exact run completed successfully;
4. the reviewed `dev` commit tree equals the current `main` tree;
5. ambiguity, missing evidence, or mismatch fails closed.

The final `main` merge commit and the promoted `dev` commit may have different commit IDs while representing the same source tree. Tree identity is the code-content proof.

## 4. Exact-SHA CI rule

A green older commit does not validate a newer head.

Incorrect:

```text
SHA A is green
new commit -> SHA B
reuse A because the diff is small
```

Correct:

```text
final dev SHA B
    |
    v
B gets its own full push CI
    |
    v
PR/release evidence refers to B
```

The exact-SHA rule applies to both human merge decisions and runtime release verification.

## 5. CI gates

The main GitHub Actions workflow includes gates such as:

```text
verify-promotion-query
browser-ui
test-cluster
        |
        +------+
               v
          test-compose
```

### verify-promotion-query

Checks the exact `dev` SHA lookup behavior the release verifier depends on.

### browser-ui

Starts a real application stack and runs Chromium regression coverage for real DOM/browser behavior.

### test-cluster

The CI cluster is fixed at three nodes: one Master, one Direct Follower, and one Relay Follower. The ten-node persistent development topology is validated separately and is not a CI node-count requirement.

Builds a real multi-node HTTPS topology and exercises role fixing, pairing, heartbeats, Storage/transport behavior, cluster control, and Follower acceptance behavior.

### test-compose

Runs/aggregates configuration/frontend syntax, source contracts, unit/runtime tests, edge/security flows, public/Admin flows, and cleanup.

A release candidate must use the successful run for the current final `dev` SHA.

## 6. GitHub release verification

The runtime verifier reads GitHub REST facts for:

- current `main` branch HEAD;
- PR association/provenance for that main commit;
- promoted source `dev` SHA;
- exact dev push workflow run;
- source and release commit trees.

### Optional authentication

A Master can use:

```dotenv
GITHUB_API_TOKEN=...
```

with least-privilege read-only access for higher REST quota.

### Caching and rate-limit behavior

FrontierCloud implements:

- normal verification caching;
- structured 403/429 rate-limit detection;
- `x-ratelimit-*` / `retry-after` parsing;
- server-side backoff until retry/reset;
- forced Admin refresh that still respects backoff;
- last-known-good verification metadata for display only.

Last-known-good evidence never sets current `publishable=true` while current verification is unavailable.

## 7. Fail-closed release authorization

When current GitHub verification is unavailable:

```text
running business service continues
cluster state remains observable
new upgrade authorization is disabled
rollback may still use local updater history
```

External verification availability is not allowed to take down an already-running service, but stale evidence is not allowed to authorize a new release.

## 8. Updater release state

The Updater reports fields such as:

```text
release_branch
current_sha
previous_sha
target_sha
state
phase
detail
```

Typical progression:

```text
validate
  |
  v
build
  |
  v
replace
  |
  v
distribute
  |
  v
complete
```

The Master coordinates local replacement and downstream Follower distribution.

## 9. System Release Management UI semantics

The UI distinguishes current production state from an actual pending transition.

Converged:

```text
<current sha> · 已与 main HEAD 一致
```

Pending upgrade:

```text
当前 <current sha> -> 待发布 <target sha>
```

The old always-present `current -> target` format was misleading after convergence because it could display the same SHA on both sides.

Release verification text may also show the promoted source `dev` SHA and CI run. Different dev/main commit IDs are expected when the trees are identical but `main` has a merge commit.

## 10. Cluster convergence

A Master reaching the target SHA is not enough.

For every required Follower verify:

```text
reachable == true
release_branch == main
current_sha == target_sha
release state compatible with successful completion
```

The UI reports cluster convergence independently from GitHub release evidence.

## 11. Maintenance mode

Maintenance protects the service while code is being replaced or the cluster is on mixed versions.

A failed or incomplete release may intentionally leave maintenance active.

Before forcing it off, inspect:

- Master Updater state/phase;
- current/target/previous SHAs;
- Follower reachability and current SHAs;
- where failure occurred: validation, build, replacement, or distribution.

Do not restore public traffic first and investigate a mixed-version cluster later.

## 12. Rollback

Rollback uses the previous Web-managed SHA stored by the Updater.

Rollback is code rollback, not automatic database-schema downgrade.

Before rolling back across a schema-changing release, verify that the older application remains compatible with the current database generation. FrontierCloud does not automatically execute destructive reverse migrations.

## 13. Schema Generation

The current schema generation is stored in:

```text
frontiercloud_schema
```

Migration history is stored in:

```text
frontiercloud_schema_migrations
```

with generation, migration name, checksum, and applied timestamp.

Migration implementation lives in:

```text
app/core/schema_migrations.py
```

## 14. New database vs existing database

### Empty new database

A new database is bootstrapped directly at the latest schema/generation.

It does not need to replay every historical migration from Generation 1.

### Existing initialized database

An existing database advances generation-by-generation through the registered migration chain.

```text
Generation N
    |
    v
Generation N+1
    |
    v
Current Generation
```

The migration registry must form a continuous chain.

## 15. MySQL DDL safety model

MySQL DDL can implicitly commit. FrontierCloud therefore does not pretend arbitrary `ALTER TABLE` sequences are transactionally reversible.

The safety model is:

```text
MySQL advisory lock
+
one-generation-at-a-time migration
+
idempotent DDL helpers
+
generation marker advances only after success
+
failed generation remains retryable
```

## 16. Migration lock

Multiple Web starts may race, so migrations use a database-scoped MySQL advisory lock.

```text
Web A / B start
   |
   v
compete for migration lock
   |
   v
A migrates
   |
   v
A advances generation
   |
   v
B later acquires lock and re-reads generation
   |
   v
already current -> no-op
```

Re-reading after lock acquisition is required because another process may have completed the migration while this process waited.

## 17. Idempotent migrations

Migration helpers check whether tables/columns/indexes already exist before applying compatible DDL.

If a process dies after part of a generation succeeds but before the marker advances, the next startup can skip already-applied pieces and continue the same generation.

The marker stays on the previous generation until the full generation succeeds.

## 18. Migration failure

When a generation fails:

- rollback is attempted where MySQL semantics permit;
- the generation marker does not advance;
- application startup fails closed;
- the operator fixes the underlying cause;
- the same idempotent generation runs again on the next startup.

Do not manually increment the schema marker to silence the failure.

## 19. Database newer than application

If the database generation is newer than the running code, startup is rejected.

FrontierCloud does not automatically perform schema downgrade.

This matters for rollback: code rollback across an incompatible schema boundary requires an explicit release/recovery plan.

## 20. Non-empty database without a marker

A non-empty database without `frontiercloud_schema` is not automatically guessed to be an older FrontierCloud generation.

It may be:

- an unknown historical commit;
- a manual database;
- a partial restore;
- a test/development database;
- unrelated data.

Blind migration is riskier than refusing startup.

## 21. Adding the next schema generation

Recommended process:

```text
1. Update latest empty-database bootstrap schema
2. Add migration N -> N+1
3. Make migration idempotent/resumable
4. Register it in the generation chain
5. Add migration regression coverage
6. Add a real MySQL upgrade smoke when practical
7. Push final change to dev
8. Wait for exact dev CI success
9. Open dev -> main PR
10. Merge after review
11. Fast-forward dev to the new main merge commit
12. Release through System Release Management
```

## 22. Merge is not deployment

Do not treat these states as equivalent:

```text
code written
CI passed
PR merged
main verified
upgrade started
Master replaced
Followers distributed
cluster converged
```

The release UI and Updater exist specifically to keep those boundaries visible.
