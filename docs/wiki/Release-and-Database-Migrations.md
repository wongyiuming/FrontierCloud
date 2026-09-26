# Release, Rollback, and Database Migrations

## 1. Branch and release workflow

The repository's normal workflow is:

```text
dev
 |
 v
full CI
 |
 v
dev -> main PR
 |
 v
manual review / merge
 |
 v
main
 |
 v
production release verification
```

Do not create additional feature/fix branches without explicit maintainer approval.

`main` is the production release authority. `dev` is the continuous development and validation branch.

## 2. Why main HEAD alone is not enough

A commit appearing on `main` is not sufficient proof that it passed the accepted development path.

FrontierCloud release verification requires:

1. the exact current `main` HEAD is associated with exactly one merged same-repository `dev -> main` PR;
2. the source PR head SHA has an exact `dev` push CI run;
3. that CI run completed successfully;
4. the PR head tree equals the current `main` HEAD tree;
5. ambiguity, missing evidence, or mismatch fails closed.

This makes release provenance independent of whether GitHub used merge, squash, or rebase as the merge method.

## 3. GitHub release verification

The release verifier reads GitHub REST data for:

- the `main` branch HEAD;
- PR association for the main commit;
- exact dev push workflow runs;
- the source dev commit/tree.

### Anonymous API rate limits

Anonymous GitHub REST requests have a small primary quota.

FrontierCloud implements:

- verification caching;
- optional authenticated `GITHUB_API_TOKEN`;
- structured 403/429 rate-limit detection;
- parsing of `x-ratelimit-*` and `retry-after`;
- server-side backoff;
- forced Admin refresh that still respects backoff;
- last-known-good verification metadata for display only.

### Fail-closed behavior

When GitHub cannot currently verify the release target:

```text
running business service continues
current cluster status remains observable
rollback may still rely on local updater history
new upgrade authorization is disabled
```

Last-known-good evidence must never authorize a new release while current verification is unavailable.

## 4. Updater release state

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

A release can be summarized as:

```text
verify
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

## 5. Maintenance mode

Maintenance mode protects the service while code is being replaced or the cluster is on mixed versions.

A failed release may intentionally leave maintenance enabled rather than pretending the cluster returned to a safe state.

When this happens, inspect:

- updater `state` and `phase`;
- target/current/previous SHAs;
- which Follower failed;
- whether failure occurred during build, replace, or distribution.

Do not force maintenance off before understanding cluster convergence.

## 6. Rollback

Rollback uses the previous Web-managed SHA stored by the Updater.

Rollback does not mean automatic database schema downgrade. Before rolling code back across a schema-changing release, verify that the older code remains compatible with the current database schema.

## 7. Schema Generation

The current database generation is stored in:

```text
frontiercloud_schema
```

The versioned migration implementation lives in:

```text
app/core/schema_migrations.py
```

Migration history is stored in:

```text
frontiercloud_schema_migrations
```

with fields:

```text
generation
migration_name
checksum
applied_at
```

## 8. New databases vs existing databases

### New empty database

A new database is bootstrapped directly at the current schema and current Generation.

It does not replay every historical migration from Generation 1.

### Existing initialized database

An existing database reads its current marker and advances one generation at a time:

```text
Generation 1
    |
    v
Generation 2
    |
    v
Generation 3
    |
    v
Current Generation
```

The migration registry must form a continuous chain.

## 9. Why there is no universal automatic ALTER

Future schema changes are not knowable in advance. The safe model is to add one explicit migration whenever the actual schema changes.

For example, if a future release adds `priority` to `cluster_worker_jobs`:

```python
async def _migration_2_to_3(conn):
    await add_column_if_missing(
        conn,
        "cluster_worker_jobs",
        "priority",
        "INT NOT NULL DEFAULT 0",
    )
```

Register it as Generation 3, and update the latest empty-database bootstrap schema to include the field directly.

## 10. MySQL DDL safety model

MySQL DDL can cause implicit commits. FrontierCloud therefore does not claim that an arbitrary sequence of `ALTER TABLE` statements can always be transactionally rolled back.

The actual safety model is:

```text
MySQL advisory lock
+
one-generation-at-a-time migration
+
idempotent DDL
+
generation marker advances only after success
+
failed generation remains retryable
```

## 11. Migration lock

Migration uses a database-scoped MySQL advisory lock so multiple Web instances cannot concurrently modify the schema.

```text
Web A / B / C start
       |
       v
compete for migration lock
       |
       v
A acquires lock
       |
       v
A completes migration and advances marker
       |
       v
B later acquires lock and re-reads generation
       |
       v
already current -> no-op
```

Re-reading the generation after acquiring the lock is required because another instance may have migrated while this instance was waiting.

## 12. Idempotent migrations

Migration helpers include:

```text
table_exists
column_exists
index_exists
add_column_if_missing
add_index_if_missing
```

If a process dies after one DDL statement succeeds but before the generation completes, the next startup can skip already-applied pieces and continue the same generation safely.

The marker remains on the previous generation until the full migration generation succeeds.

## 13. Migration failure

When a generation fails:

- the current transaction is rolled back where applicable;
- the generation marker does not advance;
- application startup fails closed;
- the operator fixes the underlying cause;
- the same idempotent generation runs again on the next startup.

This is safer than marking a partially-applied schema as upgraded.

## 14. Database is newer than the application

If:

```text
Database Generation = 6
Application Generation = 4
```

startup is rejected.

FrontierCloud does not automatically perform:

```text
6 -> 4
```

Schema downgrade is not automatic. Destructive or backward-incompatible migrations require an explicit release strategy.

## 15. Non-empty databases without a marker

A non-empty database without `frontiercloud_schema` is not automatically guessed to be an old FrontierCloud generation.

It may be:

- an unknown historical commit;
- a manually-created database;
- a partial restore;
- a development/test database;
- unrelated data.

Blind automatic `ALTER` statements would be riskier than refusing startup.

## 16. Adding the next schema generation

Recommended process:

```text
1. Update the latest bootstrap schema
2. Add migration N -> N+1
3. Make the migration idempotent
4. Register SchemaMigration
5. Add/update migration tests
6. Add a real MySQL upgrade smoke when practical
7. Run exact dev CI
8. dev -> main PR
9. Release through production controls
```

The purpose of migrations is to keep long-running production databases upgradeable without data destruction.
