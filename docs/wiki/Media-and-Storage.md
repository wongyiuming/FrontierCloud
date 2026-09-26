# Media Catalog and Storage Placement

## 1. Authoritative placement data

The relation between media resources and storage nodes is maintained in the Master MySQL global catalog.

Core tables:

```text
global_media_objects
cluster_storage_members
node_relationships
node_identity
```

Core relation:

```text
global_media_objects.storage_member_id
                |
                v
cluster_storage_members.member_id
                |
                +-- relationship_id
                         |
                         v
                  node_relationships
```

This means FrontierCloud can determine which node owns a media object without scanning every Follower filesystem.

## 2. global_media_objects

Important fields:

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

Stable business identity. Playback facts, lyric links, and other relationships should bind to stable identity rather than filename alone.

### storage_member_id

The current physical placement owner.

### object_id

The storage-member-local object identity.

### media_path

Logical media path, for example:

```text
music/artist/album/song.flac
vido/movie/example.mkv
```

### path_locator

A stable locator used for path uniqueness and conflict checks.

### state

Normal playback/catalog queries should primarily operate on:

```text
active
```

Deletion and recovery workflows may use additional intermediate states.

## 3. One logical path has one placement

FrontierCloud is not a multi-replica object store and does not split one media file into chunks across storage nodes.

A normal object looks like:

```text
song.flac
   |
   +-- Follower A
```

not:

```text
song.flac
   +-- chunk 1 -> A
   +-- chunk 2 -> B
   +-- chunk 3 -> C
```

This affects playback, deletion, recovery, and capacity accounting.

## 4. Master Local and Follower Storage

The Storage Pool can contain:

- Master Local;
- Follower A;
- Follower B;
- Follower C;

Master Local does not need a downstream `node_relationships` row. A query that wants one display endpoint for both local and remote members can use:

```sql
COALESCE(r.peer_endpoint, i.endpoint)
```

A Follower member uses `cluster_storage_members.relationship_id` to reach `node_relationships.peer_endpoint`.

## 5. Upload placement

A target member must satisfy more than one capacity field. Relevant inputs include:

- Storage enabled;
- health;
- writable;
- allocated bytes;
- used bytes;
- reserved bytes;
- physical free bytes;
- incoming object size.

Both logical allocation and real filesystem availability must remain valid.

## 6. reserved_bytes

`reserved_bytes` represents space reserved by in-progress workflows that may not yet appear as active catalog objects.

This prevents multiple concurrent uploads from each observing the same free space and collectively overcommitting a node.

Capacity analysis should not use only:

```text
allocated - used
```

without accounting for reservations and physical free space.

## 7. Catalog/filesystem mismatch

Normal operation expects catalog and managed files to agree.

If the catalog references a missing file, or a file exists without a catalog object, do not immediately edit tables or delete files by hand.

First check for:

- pending delete;
- recovery state;
- failed upload residue;
- manual file move/delete outside FrontierCloud;
- a database restore to an older point in time.

Manual filesystem changes can break deletion journal and recovery semantics.

## 8. Query every media placement

Run on the Master:

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

## 9. Active media only

Add:

```sql
WHERE g.state = 'active'
```

The normal catalog also treats active rows as the primary playable resource set.

## 10. Per-node media summary

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

`catalog_GiB` and `reported_used_GiB` are not expected to be byte-identical because the filesystem can also contain temporary/recovery data and other overhead.

## 11. Query one Follower

Replace the hostname with the real Follower endpoint:

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

## 12. Delete semantics

Managed deletion is not equivalent to `rm`.

The delete path maintains database state, quarantine/recovery boundaries, and consistency. Do not remove pending-recovery data manually just to clear a state.

If database and filesystem are seriously inconsistent, restore a consistent data/database recovery point before manually cleaning files.

## 13. Moving managed files

There is no general supported workflow where an operator moves a managed file directly on disk and expects the catalog to infer a rename.

Do not treat this as a supported rename:

```bash
mv data/media/music/A.flac data/media/music/B.flac
```

Paths interact with stable IDs, lyric relations, playback facts, placement, and delete/recovery state. Bypassing the application can create catalog drift.
