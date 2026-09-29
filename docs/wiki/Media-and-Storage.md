# Media Catalog and Storage Placement

## 1. Authoritative catalog

The Master MySQL global catalog is the authoritative mapping between logical media and physical storage placement.

Core tables include:

```text
global_media_objects
cluster_storage_members
node_relationships
node_identity
```

Core relationship:

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

FrontierCloud therefore does not need to scan every Follower filesystem to answer which node owns a managed media object.

## 2. Media identity

Important `global_media_objects` concepts:

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

Stable business identity. Playback facts, lyric relations, and other durable relationships bind to stable identity rather than filename alone.

### media_path

Logical business path, for example:

```text
music/artist/album/song.flac
vido/category/movie.mkv
```

### storage_member_id

Current physical owner in the Storage Pool.

### object_id

Storage-member-local object identity.

### path_locator

Durable uniqueness locator used by upload/path conflict logic.

### state

Normal playable/catalog objects are active. Delete/recovery workflows can use intermediate states such as `pending_delete`.

## 3. One logical path, one complete owner

FrontierCloud is not a striping/chunking filesystem and does not automatically keep multiple online media replicas.

```text
song.flac -> one storage member
```

not:

```text
song.flac -> chunk A + chunk B + chunk C on different nodes
```

This simplifies ownership, playback, delete, rename, and accounting semantics.

## 4. Storage Pool

The Storage Pool contains:

- Master Local;
- enabled Storage Followers.

Master Local does not require a downstream relationship row. Follower Storage members use `relationship_id` to reach the paired endpoint.

Playback and download resolve the current placement and use Local, Direct, or Relay transport transparently.

## 5. Admin upload site types

Admin does not ask the user to pick a concrete Storage Follower. The site selector starts empty and the operator explicitly chooses:

```text
primary
direct
relay
```

### primary

Master Local, Local transport.

### direct

An eligible Direct Follower.

### relay

An eligible Relay Follower.

For Direct/Relay, the physical member is selected by FrontierCloud according to the media-folder affinity contract below.

## 6. Placement requirements and media-folder affinity

A candidate member must be:

- in the requested site type;
- storage-enabled;
- online/healthy;
- writable;
- large enough within logical allocation;
- large enough after current reservations;
- physically able to hold the incoming object.

### Empty media folder

The immediate parent of a media file is the placement-affinity unit. When that folder has no active/pending media and no live upload reservation, the first reservation uses normal fair placement.

Selection prefers lower:

```text
(used_bytes + reserved_bytes) / allocated_bytes
```

and then more available bytes.

Selection and durable upload reservation run under the same storage write lock so concurrent Admin sessions cannot place sibling files onto different members before the first reservation becomes visible.

### Bound media folder

Once a direct child media object or a live reservation exists, the folder is bound to that storage member. Later direct-child uploads stay on the same physical member even if another same-type member is less loaded.

Example:

```text
music/Artist/Disc-1/01.mp3
music/Artist/Disc-1/02.mp3
```

Both files must share one storage member.

The binding is derived from existing `global_media_objects` and unexpired upload reservations. FrontierCloud does not add a separate folder-to-node mapping table.

A bound folder never silently spills to another member. If the owner is offline, missing, read-only, or lacks capacity, the new upload fails. If the operator chooses a different site type from the existing folder owner, the upload also fails rather than moving data or ignoring the selected type.

### Nested child folder

A child media folder is an independent affinity unit and is allowed to make a fresh fair-placement choice on its first reservation.

For example:

```text
music/Artist/Disc-1/* -> Direct Follower A
music/Artist/Disc-2/* -> Direct Follower B
```

The common ancestor does not force both child folders onto one member.

### Historical split folder

Older data may predate this rule. If the same immediate media folder is already spread across more than one storage member, FrontierCloud does not migrate those files automatically. New uploads into that split folder fail closed so the inconsistency cannot grow.

This is an upload-placement constraint. It does not change the per-object ownership model and does not introduce Follower-to-Follower storage transfer.

## 7. Capacity semantics

Do not treat all capacity numbers as interchangeable.

### Physical

```text
physical total
physical free
physical used = total - free
```

These describe the underlying filesystem.

### FrontierCloud quota

```text
allocated_bytes
used_bytes
reserved_bytes
```

These describe what FrontierCloud may use and has already committed.

Admin intentionally shows physical used/total separately from FrontierCloud used/allocated.

`available_bytes` is a scheduling/write-safety result; it is not the same thing as physical free space.

## 8. Historical ownership compatibility

The site-type UI does not rewrite historical placement. Existing:

```text
storage_member_id
member_kind
transport
```

remain the authoritative facts.

A missing transport for local/Standalone historical content is treated as primary/local presentation rather than forcing a migration.

The media-folder affinity rule also does not migrate historical split folders. It only prevents future reservations from silently extending a split placement.

## 9. Directory priority

Folders are first-class sorting objects.

Directory priority is separate from media-file priority. Public category and subcategory order is:

```text
directory priority descending -> name
```

Priority data is managed metadata and must follow a successful folder rename.

Priority-sorted catalogs are cached server-side. A cache hit must not cause a new MySQL sort query; successful priority changes invalidate the catalog generation.

## 10. Controlled folder rename

Admin supports same-parent rename for real managed `music` and `vido` directories.

Examples:

```text
music/artist -> music/artist-renamed
music/artist/live -> music/artist/concert
```

It is **not** a general cross-parent move API.

A rename is rejected when:

- the source is outside supported hierarchy;
- the target has a real managed collision;
- there is an active upload in the affected path;
- media is still pending deletion in the affected path;
- a required Follower is offline/unreachable;
- another unsafe mutation boundary is active.

On a Master, all members containing the logical directory are coordinated. If a later member or metadata commit fails, already-moved members are rolled back in reverse order.

Successful rename updates relevant metadata such as:

- media paths;
- stable-object mappings;
- playback/stat paths where applicable;
- lyric-link paths;
- visibility metadata;
- directory priority metadata.

A previously deleted target name may be reused when only stale directory metadata remains. Real media/path collisions still block the rename.

## 11. Mutation fence

Managed path operations participate in one process-local reader/writer fence.

Cross-member folder rename owns the exclusive fence from preflight through commit/rollback. Upload reservation, delete, hide/unhide, and priority mutations use the corresponding shared boundary.

Durable reservations and `pending_delete` rows remain database fences after the short in-process shared section ends.

Because this lock is process-local, production Web must run a single ASGI worker.

## 12. Visibility inheritance

Visibility is hierarchical.

An object may be:

- directly hidden at its own path;
- effectively hidden because an ancestor directory is hidden.

Admin distinguishes these states. A child that is only hidden by its parent should not expose a local "unhide" action that cannot make it public.

## 13. Managed deletion

Managed deletion is not equivalent to `rm`.

The delete flow coordinates business metadata, storage ownership, pending-delete state, local quarantine/recovery boundaries, and remote convergence.

Remote objects that cannot be deleted immediately remain represented as pending work and are retried through the convergence path. A missing remote byte can already represent successful storage-side deletion when metadata convergence is still pending.

Do not manually delete pending-recovery data simply to clear a state.

## 14. Catalog/cache consistency

Redis generation catalogs cache expensive scans and sorts. Mutable catalog APIs revalidate at the HTTP layer so a successful Admin path/visibility/priority mutation appears promptly.

Mutations invalidate the relevant server-side generation.

Cache invalidation is fail-soft **after** durable commit. A Redis failure after a committed mutation is logged and allowed to recover through TTL/generation behavior; it must not turn a committed business mutation into a client-visible false failure.

## 15. Lyrics hierarchy

Lyrics are Master-owned business content and use a bounded hierarchy:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum.

Every surface must agree on the same boundary:

- upload;
- validation;
- Admin tree;
- scoped search;
- download/delete collection;
- lyric catalog;
- relation management.

## 16. Folder lyric upload and relation browser

Admin supports uploading a folder of LRC files while preserving supported relative structure.

The lyric relation browser can browse both sides hierarchically and displays recursive file counts:

- lyric folders -> `N 份歌词`;
- music folders -> `N 首曲目`.

`lyrics/default.lrc` is excluded from user-visible lyric counts.

## 17. Same-name auto-link

Auto-link matches exact same-stem music and LRC files.

Rules:

1. prefer the same relative album/path context;
2. if multiple candidates remain ambiguous, report ambiguity rather than guessing;
3. fill missing/default fallback relations only;
4. never overwrite an explicit user-selected business lyric.

The explicit-relation check and replacement happen under the database lock required to prevent a concurrent manual choice from being overwritten.

## 18. Internal default lyric

`lyrics/default.lrc` is runtime-maintained fallback content.

It is:

- excluded from Admin lyric tree/search/counts;
- excluded from user-managed relation counts;
- not a normal delete/download target;
- not evidence that a track has an explicit user lyric;
- generated/repaired by runtime code rather than tracked under `data/` in Git.

## 19. Query media placement

Run on the Master:

```sql
SELECT
    g.media_path,
    g.object_kind,
    ROUND(g.size_bytes / 1024 / 1024, 2) AS size_MiB,
    g.state,
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
WHERE g.state = 'active'
```

## 20. Catalog/filesystem mismatch

If catalog and bytes disagree, first investigate:

- pending delete;
- upload reservation/cleanup;
- recovery/quarantine state;
- a failed remote operation;
- direct filesystem modification outside FrontierCloud;
- a database/filesystem restore from different recovery points.

Do not immediately edit tables or move/delete managed files by hand. Restore a consistent business/data recovery point when necessary.
