# Bash / SQL Command Reference

> Examples assume the repository is available at `/root/FrontierCloud`. Adjust the path for your deployment.

## 1. Compose status

```bash
cd /root/FrontierCloud
docker compose ps
```

Validate configuration:

```bash
docker compose config --quiet
```

## 2. Logs

```bash
docker compose logs -f --tail=200 web
docker compose logs -f --tail=200 updater
docker compose logs -f --tail=200 nginx
docker compose logs -f --tail=200 mysql
docker compose logs -f --tail=200 redis
```

## 3. Health

```bash
curl -fsS http://127.0.0.1/health/live && echo
curl -fsS http://127.0.0.1/health/ready && echo
```

Use the real HTTPS hostname on a production fixed-role node.

## 4. Repository / deployed source identity

```bash
git rev-parse HEAD
git branch --show-current
git status --short
```

Before assuming a UI fix is missing, verify the running release SHA in System Release Management and compare it with repository `main`.

## 5. Generated Admin Key

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
```

## 6. Metrics token

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

## 7. MySQL application password

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/mysql_password'
```

## 8. Open MySQL interactively

```bash
docker compose exec mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
'
```

Non-interactive SQL:

```bash
docker compose exec -T mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
' <<'SQL'
SELECT NOW();
SQL
```

## 9. Node identity

```sql
SELECT * FROM node_identity;
```

## 10. Relationships

```sql
SELECT
    relationship_id,
    peer_id,
    peer_endpoint,
    direction,
    mode,
    state,
    status,
    last_heartbeat,
    rtt_ms,
    failures,
    recoveries,
    peer_version,
    protocol
FROM node_relationships
ORDER BY peer_endpoint;
```

## 11. Storage members

```sql
SELECT
    member_id,
    relationship_id,
    member_kind,
    storage_enabled,
    transport,
    health,
    writable,
    ROUND(allocated_bytes / 1024 / 1024 / 1024, 3) AS allocated_GiB,
    ROUND(used_bytes / 1024 / 1024 / 1024, 3) AS used_GiB,
    ROUND(reserved_bytes / 1024 / 1024 / 1024, 3) AS reserved_GiB,
    ROUND(physical_free_bytes / 1024 / 1024 / 1024, 3) AS physical_free_GiB,
    updated_at
FROM cluster_storage_members
ORDER BY member_kind, member_id;
```

`physical_total_bytes` is a live observation exposed through the current storage/heartbeat summary rather than something to assume is present as the same persisted table column on every historical schema.

## 12. Media-to-storage placement

```sql
SELECT
    g.media_path,
    g.object_kind AS type,
    ROUND(g.size_bytes / 1024 / 1024, 2) AS size_MiB,
    g.state AS media_state,
    g.storage_member_id,
    s.member_kind,
    COALESCE(r.peer_endpoint, i.endpoint) AS storage_endpoint,
    s.transport,
    s.health,
    s.writable,
    g.media_id,
    g.object_id
FROM global_media_objects AS g
JOIN cluster_storage_members AS s
  ON s.member_id = g.storage_member_id
LEFT JOIN node_relationships AS r
  ON r.relationship_id = s.relationship_id
CROSS JOIN node_identity AS i
ORDER BY g.storage_member_id, g.media_path;
```

Active objects only:

```sql
SELECT
    g.media_path,
    g.storage_member_id,
    COALESCE(r.peer_endpoint, i.endpoint) AS endpoint,
    g.size_bytes,
    g.media_id
FROM global_media_objects AS g
JOIN cluster_storage_members AS s
  ON s.member_id = g.storage_member_id
LEFT JOIN node_relationships AS r
  ON r.relationship_id = s.relationship_id
CROSS JOIN node_identity AS i
WHERE g.state='active'
ORDER BY endpoint, g.media_path;
```

## 13. Pending-delete media

```sql
SELECT
    media_id,
    media_path,
    storage_member_id,
    object_id,
    size_bytes,
    updated_at
FROM global_media_objects
WHERE state='pending_delete'
ORDER BY updated_at;
```

Do not delete these rows by hand merely to unblock Admin. First diagnose the storage-side convergence failure.

## 14. Active media totals per Storage Member

```sql
SELECT
    s.member_id,
    COALESCE(r.peer_endpoint, i.endpoint) AS endpoint,
    COUNT(g.media_id) AS media_count,
    ROUND(COALESCE(SUM(g.size_bytes), 0) / 1024 / 1024 / 1024, 3) AS catalog_GiB,
    ROUND(s.used_bytes / 1024 / 1024 / 1024, 3) AS used_GiB,
    ROUND(s.allocated_bytes / 1024 / 1024 / 1024, 3) AS allocated_GiB,
    ROUND(s.reserved_bytes / 1024 / 1024 / 1024, 3) AS reserved_GiB,
    ROUND(s.physical_free_bytes / 1024 / 1024 / 1024, 3) AS physical_free_GiB
FROM cluster_storage_members AS s
LEFT JOIN global_media_objects AS g
  ON g.storage_member_id=s.member_id
 AND g.state='active'
LEFT JOIN node_relationships AS r
  ON r.relationship_id=s.relationship_id
CROSS JOIN node_identity AS i
GROUP BY
    s.member_id, r.peer_endpoint, i.endpoint,
    s.used_bytes, s.allocated_bytes, s.reserved_bytes, s.physical_free_bytes
ORDER BY catalog_GiB DESC;
```

## 15. Media on one Follower

Replace `example.com` with the actual Follower endpoint:

```sql
SELECT
    g.media_path,
    g.object_kind,
    ROUND(g.size_bytes / 1024 / 1024, 2) AS size_MiB,
    g.state,
    g.media_id,
    g.object_id,
    r.peer_endpoint
FROM global_media_objects AS g
JOIN cluster_storage_members AS s
  ON s.member_id=g.storage_member_id
JOIN node_relationships AS r
  ON r.relationship_id=s.relationship_id
WHERE r.peer_endpoint LIKE '%example.com%'
ORDER BY g.media_path;
```

## 16. Upload sessions / path reservations

Inspect recent sessions before manually changing anything:

```sql
SELECT
    upload_id,
    path_locator,
    state,
    created_at,
    updated_at,
    expires_at
FROM cluster_upload_sessions
ORDER BY updated_at DESC
LIMIT 100;
```

If the exact table name differs on an older schema, inspect the current schema rather than inventing a cleanup query. Completed sessions should not retain a path lease; expired reserved sessions are handled by the application cleanup path.

## 17. Directory metadata / priority

Directory priority is stored as managed sorting metadata with `object_kind='directory'`. To understand a priority/rename collision, inspect the current schema and the affected logical path rather than deleting metadata blindly.

Useful source locations:

```text
app/services/media_directories.py
app/services/media_directory_catalog.py
```

## 18. Lyric relations

The relation table is:

```text
media_lyric_links
```

Inspect recent/current links with schema-aware columns:

```sql
SELECT *
FROM media_lyric_links
ORDER BY updated_at DESC
LIMIT 100;
```

`lyrics/default.lrc` is internal fallback content; do not count it as proof of a user-managed relation.

## 19. Find default fallback relations

```sql
SELECT *
FROM media_lyric_links
WHERE lyric_path='lyrics/default.lrc';
```

These are fallback state, not normal user-selected business lyrics.

## 20. Backup members

```sql
SELECT
    member_id,
    enabled,
    generation,
    last_success,
    lag_seconds,
    checksum,
    state,
    updated_at
FROM cluster_backup_members
ORDER BY member_id;
```

## 21. Backup recovery points

```sql
SELECT
    master_id,
    generation,
    ROUND(size_bytes / 1024 / 1024, 2) AS size_MiB,
    chunk_count,
    state,
    checksum,
    created_at,
    updated_at
FROM cluster_business_backups
ORDER BY generation DESC
LIMIT 50;
```

If a stale generation is `receiving`, inspect application logs and the abort/retry path before deleting rows.

## 22. Historical Compute tables

Compute Worker is retired. If historical tables such as `cluster_compute_members` or `cluster_worker_jobs` still exist, treat them as compatibility/legacy data rather than active product state.

Do not use old Worker-job queries as an operational health check for current FrontierCloud.

## 23. Schema Generation

```sql
SELECT * FROM frontiercloud_schema;
```

Migration history:

```sql
SELECT
    generation,
    migration_name,
    checksum,
    applied_at
FROM frontiercloud_schema_migrations
ORDER BY generation;
```

Never manually advance the generation marker to hide a failed migration.

## 24. GitHub API rate-limit diagnostics

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
    "retry-after",
):
    print(name + ":", response.headers.get(name))
reset = response.headers.get("x-ratelimit-reset")
if reset:
    print("reset in:", max(0, int(reset) - int(time.time())), "seconds")
print(response.text[:1000])
PY
```

If production regularly approaches anonymous quota, configure a least-privilege read-only `GITHUB_API_TOKEN` on the Master.

## 25. Containers and images

```bash
docker compose ps
```

```bash
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}' \
  | grep -E 'frontiercloud|REPOSITORY'
```

## 26. Disk usage

```bash
df -h
```

Docker disk usage:

```bash
docker system df
```

## 27. Check the exact dev/main relationship

```bash
git fetch origin dev main
git rev-parse origin/dev
git rev-parse origin/main
git merge-base origin/dev origin/main
```

After a `dev -> main` release PR merges, `dev` should be fast-forwarded to the resulting `main` merge commit before the next development cycle.

Do not force-update either branch.

## 28. Local test baseline

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
docker compose config --quiet
```

The exact final `dev` push CI is still the authoritative release evidence.

## 29. Commands that are not routine troubleshooting tools

Do not use these as casual fixes:

```bash
docker compose down --volumes
rm -rf data/*
docker system prune -a --volumes
```

Also avoid:

- direct `mv` / `rm` on managed media;
- manual deletion of pending-delete rows;
- manual upload-lease cleanup without understanding state;
- manual Schema Generation edits;
- enabling multiple Web workers;
- force-pushing `dev` or `main`.
