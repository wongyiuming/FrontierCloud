# Operations and Troubleshooting

## 1. Troubleshooting order

Do not start an incident by restarting every container. Diagnose by layer:

```text
1. Is business traffic actually unavailable?
2. Nginx / Web readiness
3. MySQL / Redis
4. Node relationships and heartbeats
5. Storage / Compute / Backup state
6. Updater / release state
7. External dependencies such as GitHub
```

These dimensions are intentionally independent. A GitHub release-verification failure does not imply playback failure, and a Backup failure does not imply that Storage is unavailable.

## 2. Fast health check

```bash
docker compose ps
```

```bash
curl -fsS http://127.0.0.1/health/live
curl -fsS http://127.0.0.1/health/ready
```

Use the real HTTPS hostname in production.

Recent logs:

```bash
docker compose logs --tail=200 web
docker compose logs --tail=200 nginx
docker compose logs --tail=200 updater
docker compose logs --tail=200 mysql
docker compose logs --tail=200 redis
```

## 3. Business works but the release page reports an error

Separate business/data-plane health from external release-verification health.

When GitHub REST is rate limited:

- the current Master/Follower business service can continue;
- already-running versions are unaffected;
- new upgrades fail closed;
- Admin reports GitHub verification as unavailable/rate limited;
- the Updater itself may still be healthy.

### Check GitHub API rate limit from the Web container

```bash
docker compose exec -T web python - <<'PY'
import httpx
import time

url = "https://api.github.com/repos/wongyiuming/FrontierCloud/branches/main"
with httpx.Client(
    trust_env=False,
    timeout=httpx.Timeout(5, connect=3),
    headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "FrontierCloud-release-control",
    },
) as client:
    response = client.get(url)

print("HTTP:", response.status_code)
for name in (
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-used",
    "x-ratelimit-reset",
):
    print(name + ":", response.headers.get(name))
reset = response.headers.get("x-ratelimit-reset")
if reset:
    print("reset in:", max(0, int(reset) - int(time.time())), "seconds")
print(response.text[:1000])
PY
```

If the result is:

```text
HTTP: 403
x-ratelimit-remaining: 0
```

then the GitHub API quota is exhausted; this is not equivalent to general connectivity to `github.com` being down.

For production, configure a least-privilege read-only token on the Master:

```dotenv
GITHUB_API_TOKEN=...
```

## 4. Follower is Offline

Check:

1. `last_heartbeat` freshness;
2. RTT;
3. consecutive failures;
4. HTTPS certificate validity;
5. configured peer endpoint;
6. Follower Web health;
7. DNS, routing, and firewall between nodes.

One high RTT sample is not enough to classify a node as offline. Heartbeat freshness and repeated failures matter more.

## 5. A toggle is enabled but not effective

Inspect Desired and Observed state.

Common causes:

- Follower is offline;
- the next control heartbeat has not completed yet;
- the Follower rejected the configuration;
- version/protocol mismatch;
- TLS or relationship state is invalid.

"Configuration saved" must not be interpreted as "Follower applied it".

## 6. Storage troubleshooting

Important fields:

```text
storage_enabled
health
writable
allocated_bytes
used_bytes
reserved_bytes
physical_free_bytes
```

### Logical space exists but upload is rejected

Possible causes:

- insufficient physical free space;
- reservations already consume the remaining allowance;
- unhealthy node;
- `writable=false`;
- member state changed after the placement decision was prepared.

### Catalog size differs from filesystem usage

`SUM(global_media_objects.size_bytes)` and a member's reported `used_bytes` do not need to be identical.

If the gap is unexpectedly large, inspect:

- in-progress uploads;
- pending delete;
- recovery/quarantine data;
- manually-added files;
- stale temporary data.

## 7. Compute troubleshooting

Inspect:

- `worker_slots`;
- running count;
- queued count;
- available slots;
- CPU;
- available memory;
- capabilities;
- `attempts`;
- lease expiration.

### Why did this node not receive a job?

Check:

1. Compute is enabled and effective;
2. capability matches the job;
3. a slot is available;
4. the job is not pinned to another member;
5. another capable Follower did not lease it first;
6. the job is not already completed/failed.

Ordinary shared jobs currently use capability + FIFO semantics, not "lowest CPU wins" scoring.

## 8. Backup troubleshooting

Inspect:

- Enabled / Effective;
- `last_success`;
- most recent attempt/result;
- generation;
- checksum;
- recovery-point count;
- next planned attempt.

A transient `pending`-like state must not override the durable meaning of a known successful recovery point. Use `last_success` and concrete recovery-point metadata as the primary evidence.

After a failed attempt, current logic retries on a shorter failure interval rather than waiting for the full normal backup period.

## 9. MySQL startup / schema problems

### Database Generation is behind the application

The application should migrate the initialized database generation-by-generation.

### Migration fails

Startup fails closed and the generation marker does not advance. Fix the underlying problem and restart; idempotent migration logic allows the same generation to be retried safely.

### Database Generation is newer than the application

An older application is reading a database upgraded by newer code. FrontierCloud rejects startup and does not attempt automatic schema downgrade.

### Non-empty database has no FrontierCloud schema marker

FrontierCloud does not guess an unknown historical schema and run blind `ALTER` statements. Determine the database origin before attempting recovery/adoption.

## 10. Updater is stuck

```bash
docker compose logs --tail=300 updater
```

Inspect release fields such as:

```text
state
phase
target_sha
current_sha
previous_sha
detail
```

If maintenance mode is active, first determine whether a release is still running or has failed. Do not delete the maintenance-state volume to hide the symptom.

## 11. Cluster versions differ

A successful Master replacement does not prove full cluster convergence.

For every Follower confirm:

```text
reachable == true
release_branch == main
current_sha == target_sha
state is compatible with successful/idle completion
```

If only part of the cluster upgrades, maintenance mode is a safety boundary. Do not restore public traffic first and investigate version drift later.

## 12. Delete/recovery problems

Managed deletion has database-journal and recovery semantics.

Do not use direct `rm -rf` to clear pending delete/recovery state.

If recovery blocks mutations, inspect Web logs and database state, and verify that MySQL and `./data` belong to the same recovery point. When necessary, restore a consistent set of database, file data, and secrets.

## 13. Backup strategy

At minimum, production recovery must cover:

```text
MySQL
./data
runtime_secrets
```

Treat them as one recovery asset set. Restoring only the database or only files can create catalog/filesystem drift.

## 14. Incident evidence collection

Capture evidence before changing configuration:

```bash
date -Is
docker compose ps
docker compose logs --tail=300 web > /tmp/frontier-web.log
docker compose logs --tail=300 updater > /tmp/frontier-updater.log
docker compose logs --tail=300 nginx > /tmp/frontier-nginx.log
```

For schema/database incidents:

```bash
docker compose exec -T mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)";
export MYSQL_PWD;
mysql -u "$MYSQL_USER" "$MYSQL_DATABASE" -e "SELECT * FROM frontiercloud_schema; SELECT * FROM frontiercloud_schema_migrations ORDER BY generation;"
'
```

Preserve the incident state before applying a fix.
