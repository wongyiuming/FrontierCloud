# Bash / SQL Command Reference

> Examples assume the repository is available at `/root/FrontierCloud`. Adjust the path if the deployment lives elsewhere.

## 1. Compose status

```bash
cd /root/FrontierCloud
docker compose ps
```

Validate Compose configuration:

```bash
docker compose config --quiet
```

## 2. Logs

```bash
docker compose logs -f --tail=200 web
```

```bash
docker compose logs -f --tail=200 updater
```

```bash
docker compose logs -f --tail=200 nginx
```

```bash
docker compose logs -f --tail=200 mysql
```

## 3. Health

```bash
curl -fsS http://127.0.0.1/health/live && echo
curl -fsS http://127.0.0.1/health/ready && echo
```

Use the real HTTPS hostname in production.

## 4. Admin Key

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
```

## 5. Metrics token

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

## 6. Open MySQL

```bash
docker compose exec mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
'
```

Run non-interactive SQL:

```bash
docker compose exec -T mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
' <<'SQL'
SELECT NOW();
SQL
```

## 7. Node identity

```sql
SELECT * FROM node_identity;
```

## 8. Relationships

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

## 9. Storage members

```sql
SELECT
    member_id,
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

## 10. Media-to-storage placement detail

```sql
SELECT
    g.media_path,
    g.object_kind AS type,
    ROUND(g.size_bytes / 1024 / 1024, 2) AS size_MiB,
    g.state AS media_state,
    g.storage_member_id AS storage_node_id,
    s.member_kind AS node_kind,
    COALESCE(r.peer_endpoint, i.endpoint) AS storage_endpoint,
    s.transport,
    s.health AS node_health,
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

## 11. Active media totals per Storage Member

```sql
SELECT
    s.member_id,
    COALESCE(r.peer_endpoint, i.endpoint) AS endpoint,
    COUNT(g.media_id) AS media_count,
    ROUND(COALESCE(SUM(g.size_bytes), 0) / 1024 / 1024 / 1024, 3) AS catalog_GiB
FROM cluster_storage_members AS s
LEFT JOIN global_media_objects AS g
    ON g.storage_member_id = s.member_id
   AND g.state='active'
LEFT JOIN node_relationships AS r
    ON r.relationship_id = s.relationship_id
CROSS JOIN node_identity AS i
GROUP BY s.member_id, r.peer_endpoint, i.endpoint
ORDER BY catalog_GiB DESC;
```

## 12. Media on one Follower

Replace `example.com` with the actual endpoint:

```sql
SELECT
    g.media_path,
    g.object_kind,
    ROUND(g.size_bytes / 1024 / 1024, 2) AS size_MiB,
    g.state,
    g.media_id,
    g.object_id
FROM global_media_objects AS g
JOIN cluster_storage_members AS s
    ON s.member_id = g.storage_member_id
JOIN node_relationships AS r
    ON r.relationship_id = s.relationship_id
WHERE r.peer_endpoint LIKE '%example.com%'
ORDER BY g.media_path;
```

## 13. Compute members

```sql
SELECT
    member_id,
    enabled,
    worker_slots,
    available_slots,
    cpu_percent,
    ROUND(memory_available_bytes / 1024 / 1024 / 1024, 3) AS memory_available_GiB,
    capabilities,
    updated_at
FROM cluster_compute_members
ORDER BY member_id;
```

## 14. Recent Worker jobs

```sql
SELECT
    job_id,
    job_type,
    media_id,
    member_id,
    state,
    attempts,
    lease_expires_at,
    created_at,
    updated_at,
    result
FROM cluster_worker_jobs
ORDER BY updated_at DESC
LIMIT 100;
```

Non-completed jobs:

```sql
SELECT
    job_id,
    job_type,
    media_id,
    member_id,
    state,
    attempts,
    created_at,
    updated_at
FROM cluster_worker_jobs
WHERE state NOT IN ('completed', 'success')
ORDER BY created_at;
```

## 15. Backup members

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

## 16. Backup recovery points

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

## 17. Schema Generation

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

## 18. GitHub API rate-limit diagnostics

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
print("x-ratelimit-limit:", response.headers.get("x-ratelimit-limit"))
print("x-ratelimit-remaining:", response.headers.get("x-ratelimit-remaining"))
print("x-ratelimit-used:", response.headers.get("x-ratelimit-used"))
print("x-ratelimit-reset:", response.headers.get("x-ratelimit-reset"))
reset = response.headers.get("x-ratelimit-reset")
if reset:
    print("reset in:", max(0, int(reset) - int(time.time())), "seconds")
print(response.text[:1000])
PY
```

## 19. Repository commit/branch

```bash
git rev-parse HEAD
git branch --show-current
git status --short
```

## 20. Containers and images

```bash
docker compose ps
```

```bash
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}' | grep -E 'frontiercloud|REPOSITORY'
```

## 21. Disk usage

```bash
df -h
```

Docker usage:

```bash
docker system df
```

## 22. Commands that are not normal troubleshooting tools

Do not use these as routine fixes unless you are deliberately performing disaster recovery:

```bash
docker compose down --volumes
rm -rf data/*
docker system prune -a --volumes
```

Do not directly `mv`/`rm` managed media to bypass the catalog.
