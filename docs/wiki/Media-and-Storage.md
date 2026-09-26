# 媒体目录与存储 Placement

## 1. 权威关系在哪里

FrontierCloud 的媒体与存储节点关系由 Master MySQL 的全局目录维护。

核心表：

```text
global_media_objects
cluster_storage_members
node_relationships
node_identity
```

核心关联：

```text
global_media_objects.storage_member_id
                │
                ▼
cluster_storage_members.member_id
                │
                ├── member_kind
                ├── transport
                ├── health
                ├── writable
                └── relationship_id
                         │
                         ▼
                  node_relationships
                         │
                         ├── peer_id
                         └── peer_endpoint
```

因此“一个媒体资源实际在哪个节点”可以直接从数据库确定，不需要扫描所有 Follower 文件系统。

## 2. global_media_objects

关键字段：

```text
media_id
storage_member_id
object_id
media_path
path_locator
object_kind
size_bytes
etag
state
created_at
updated_at
```

### media_id

业务稳定标识。播放统计、歌词绑定等业务关系应尽量依赖稳定 ID，而不是仅依赖文件名。

### storage_member_id

媒体当前实际 placement。

### object_id

存储成员内部对象标识。

### media_path

业务逻辑路径，例如：

```text
music/artist/album/song.flac
vido/movie/example.mkv
```

### path_locator

用于路径唯一定位和冲突检查。

### state

常见业务查询应优先关注：

```text
active
```

删除/恢复流程可能出现其他中间状态，不要把非 active 行当作正常可播放媒体。

## 3. 一条路径只有一个 Placement

当前模型不是对象多副本系统，也不是分布式分片文件系统。

一个逻辑媒体路径对应一个完整对象，并放在一个 Storage Member。

因此：

```text
song.flac
   │
   └── Follower A
```

而不是：

```text
song.flac
   ├── chunk 1 → A
   ├── chunk 2 → B
   └── chunk 3 → C
```

这对删除、播放、恢复和容量统计都很重要。

## 4. Master Local 与 Follower Storage

Storage Pool 可以同时包含：

- Master Local；
- Follower A；
- Follower B；
- Follower C；

Master Local 没有下游 `node_relationships`，所以查询 endpoint 时通常需要：

```sql
COALESCE(r.peer_endpoint, i.endpoint)
```

Follower 则通过 `cluster_storage_members.relationship_id` 关联到 `node_relationships.peer_endpoint`。

## 5. 上传 Placement

上传不是简单“找第一个节点”。在选择 Storage Member 时需要关注：

- Storage 是否 enabled；
- health；
- writable；
- allocated bytes；
- used bytes；
- reserved bytes；
- physical free bytes；
- 上传文件大小。

逻辑容量与真实磁盘容量都必须满足要求。

## 6. reserved_bytes

`reserved_bytes` 用于表达已经为进行中的上传/流程预留的空间。

这可以避免两个并发上传同时看到“还有 10 GiB”，各自都尝试写 8 GiB，最终把节点打满。

在分析容量时不要只看：

```text
allocated - used
```

还要考虑 reserved 和 physical free。

## 7. Catalog 与文件系统不一致

正常情况下，数据库目录与节点文件应一致。

如果出现：

```text
Catalog 有对象，但文件不存在
```

或者：

```text
文件存在，但 Catalog 无对象
```

不要直接手工修表或删除文件。

应先判断：

- 是否存在 pending delete；
- 是否处于 recovery；
- 是否是失败上传残留；
- 是否是人工绕过系统移动/删除造成；
- 数据库是否曾恢复到旧时间点。

手工改文件系统可能破坏删除 journal 和 recovery 语义。

## 8. 查询媒体与节点明细

在 Master 上：

```bash
docker compose exec -T mysql sh -lc '
MYSQL_PWD="$(cat /run/frontiercloud-secrets/mysql_password)"
export MYSQL_PWD
exec mysql --default-character-set=utf8mb4 -u "$MYSQL_USER" "$MYSQL_DATABASE"
' <<'SQL'
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
SQL
```

## 9. 只看 active 媒体

增加：

```sql
WHERE g.state = 'active'
```

Catalog 对正常播放资源的主要查询也以 active 为准。

## 10. 每个节点的媒体汇总

```sql
SELECT
    s.member_id AS storage_node_id,
    s.member_kind AS node_kind,
    COALESCE(r.peer_endpoint, i.endpoint) AS endpoint,
    s.health,
    s.writable,
    COUNT(g.media_id) AS media_count,
    ROUND(COALESCE(SUM(g.size_bytes), 0) / 1024 / 1024 / 1024, 3) AS catalog_GiB,
    ROUND(s.used_bytes / 1024 / 1024 / 1024, 3) AS reported_used_GiB,
    ROUND(s.allocated_bytes / 1024 / 1024 / 1024, 3) AS allocated_GiB,
    ROUND(s.reserved_bytes / 1024 / 1024 / 1024, 3) AS reserved_GiB,
    ROUND(s.physical_free_bytes / 1024 / 1024 / 1024, 3) AS physical_free_GiB
FROM cluster_storage_members AS s
LEFT JOIN global_media_objects AS g
    ON g.storage_member_id = s.member_id
   AND g.state = 'active'
LEFT JOIN node_relationships AS r
    ON r.relationship_id = s.relationship_id
CROSS JOIN node_identity AS i
GROUP BY
    s.member_id, s.member_kind, r.peer_endpoint, i.endpoint,
    s.health, s.writable, s.used_bytes, s.allocated_bytes,
    s.reserved_bytes, s.physical_free_bytes
ORDER BY catalog_GiB DESC;
```

`catalog_GiB` 和 `reported_used_GiB` 不要求完全相等。底层文件系统还可能包含临时数据、目录结构、恢复数据或其他运行开销。

## 11. 查某个 Follower

例如：

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
    ON s.member_id = g.storage_member_id
JOIN node_relationships AS r
    ON r.relationship_id = s.relationship_id
WHERE r.peer_endpoint LIKE '%example.com%'
ORDER BY g.media_path;
```

## 12. 删除语义

受管媒体删除不是简单 `rm`。

删除流程需要维护数据库状态、临时隔离和恢复边界。出现 pending recovery 时不要手工删除目录来“解锁”。

如果数据库与文件系统发生灾难性不一致，应先恢复数据库/备份并检查 recovery 逻辑，而不是先清理文件。

## 13. 移动文件

当前没有通用的“直接在文件系统移动媒体然后自动认领新路径”的设计。

不要：

```bash
mv data/media/music/A.flac data/media/music/B.flac
```

然后期待数据库自动把它当作合法 rename。

业务路径与稳定 ID、歌词、播放统计、placement 等存在关联，直接绕过 API 会制造目录漂移。
