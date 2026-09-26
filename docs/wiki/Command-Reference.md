# 常用 Bash / SQL

> 默认假设项目位于 `/root/FrontierCloud`。如果实际路径不同，请先进入项目目录或修改 `-f` 路径。

## 1. Compose 状态

```bash
cd /root/FrontierCloud
docker compose ps
```

完整配置校验：

```bash
docker compose config --quiet
```

## 2. 日志

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

## 3. 健康检查

```bash
curl -fsS http://127.0.0.1/health/live && echo
curl -fsS http://127.0.0.1/health/ready && echo
```

HTTPS 生产环境请改为真实域名。

## 4. 读取 Admin Key

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
```

## 5. 读取 metrics token

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

## 6. 进入 MySQL

```bash
docker compose exec mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
'
```

非交互执行 SQL：

```bash
docker compose exec -T mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
' <<'SQL'
SELECT NOW();
SQL
```

## 7. 查看节点身份

```sql
SELECT * FROM node_identity;
```

## 8. 查看节点关系

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

## 9. Storage Member 汇总

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

## 10. 媒体 ↔ Storage Node 明细

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

只看 active：

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

## 11. 每个 Storage Node 的 active 媒体量

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

## 12. 查指定 Follower 的媒体

把域名替换成实际 Follower：

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

## 13. Compute Member

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

## 14. 最近 Worker Jobs

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

只看未完成：

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

## 15. Backup Member

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

## 16. Backup 恢复点

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

迁移历史：

```sql
SELECT
    generation,
    migration_name,
    checksum,
    applied_at
FROM frontiercloud_schema_migrations
ORDER BY generation;
```

## 18. GitHub API 限流诊断

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
) as c:
    r = c.get(url)

print("HTTP:", r.status_code)
print("x-ratelimit-limit:", r.headers.get("x-ratelimit-limit"))
print("x-ratelimit-remaining:", r.headers.get("x-ratelimit-remaining"))
print("x-ratelimit-used:", r.headers.get("x-ratelimit-used"))
print("x-ratelimit-reset:", r.headers.get("x-ratelimit-reset"))
reset = r.headers.get("x-ratelimit-reset")
if reset:
    print("reset in:", max(0, int(reset) - int(time.time())), "seconds")
print(r.text[:1000])
PY
```

## 19. 当前 Git Commit

宿主机仓库：

```bash
git rev-parse HEAD
git branch --show-current
git status --short
```

## 20. 当前容器与镜像

```bash
docker compose ps
```

```bash
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}' | grep -E 'frontiercloud|REPOSITORY'
```

## 21. 磁盘空间

```bash
df -h
```

Docker 占用：

```bash
docker system df
```

## 22. 不建议直接执行的命令

除非明确做灾难恢复，不要把以下命令当作日常排障：

```bash
docker compose down --volumes
rm -rf data/*
docker system prune -a --volumes
```

也不要直接对受管媒体文件执行 `mv/rm` 来绕过 Catalog。
