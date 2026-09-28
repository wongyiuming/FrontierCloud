# Architecture

## 1. System model

FrontierCloud supports Standalone operation and a fixed Master/Follower cluster model.

```text
Browser
  |
  v
Nginx
  |
  v
FastAPI / Web
  |
  +---- MySQL / Redis
  |
  +---- Updater
  |
  +---- authenticated cluster control/data plane
          |
          +---- Follower Storage
          +---- Follower Backup
```

A cluster has exactly one business Master. Followers are resource nodes, not secondary business authorities.

The frontend is native HTML/CSS/JavaScript. Karaoke reuses the browser media stack and the project's existing Rust/WASM audio module.

## 2. Runtime components

FrontierCloud is designed for a one-core VPS. Product requests may stream bounded chunks and perform ordinary control-plane work, but the runtime must not introduce burst or sustained high-load computation. Transcoding, compression, inference, bulk transformation, child processes, executor offload, and compute-heavy dependencies are rejected at review and in CI. CPU recovery checks detect runaway work; they do not authorize computation below a numeric ceiling.

| Component | Purpose |
| --- | --- |
| Nginx | HTTP/HTTPS entry, static delivery, proxying, maintenance mode, edge/IP enforcement, internal route limits |
| Web | FastAPI business API, Admin, media/catalog logic, cluster control, lyrics, playback, observability |
| MySQL | Durable business facts, media catalog, relationships, audit, schema generation/migrations |
| Redis | Runtime catalog cache and coordination data; not the sole durable source of business truth |
| Updater | Web-managed upgrade/rollback, image build/replace, cluster release coordination |
| STUN / Coturn | Required WebRTC network observation |
| media-init / secrets-init | Data-tree permissions and persistent runtime-secret initialization |

## 3. Roles and authority

### Standalone

Standalone is a full single-node business mode. HTTP remains supported. It owns its local business/data state without cluster relationships.

### Master

The Master owns the business truth for a cluster, including:

- global media catalog and placement;
- media identity and lifecycle facts;
- lyrics and lyric relations;
- playback facts and sorting preferences;
- karaoke account/recording business state;
- Admin audit state;
- node desired configuration;
- release coordination and cluster convergence.

### Follower

A Follower owns only the local resource state needed to provide authenticated services to its Master. It may provide:

- media Storage;
- business Backup recovery points;
- health/metrics observations;
- signed Direct/Relay data-plane operations;
- authenticated node-control endpoints.

It must not become an alternate business source of truth.

### Compute retirement

Compute Worker is retired. Worker slots, task leasing, scheduling, worker UI, and Compute product configuration are no longer active runtime capabilities. Historical tables/data may remain for compatibility and backup safety, but new work must not accidentally revive the product surface.

## 4. TLS and trust boundary

Fixed Master/Follower roles require certificate-verified HTTPS. Loss of the required TLS/certificate conditions fails closed rather than silently downgrading a fixed node back to Standalone or accepting insecure cluster control.

Public business traffic is served by the Master. Followers expose only the authenticated resource/control surfaces intended for their Master relationship.

## 5. Global media identity and placement

The Master global catalog records the authoritative mapping from logical media identity to physical placement.

```text
global_media_objects
  media_id
  media_path
  storage_member_id
  object_id
  path_locator
  object_kind
  size_bytes
  state
        |
        v
cluster_storage_members
  member_id
  member_kind
  relationship_id
  transport
  storage_enabled
  allocated_bytes
  used_bytes
  reserved_bytes
  physical_free_bytes
  health
  writable
```

A normal media object has one logical path and one complete physical owner. FrontierCloud is not a cross-node chunking or automatic replication filesystem.

Stable `media_id` is the business identity. Playback statistics, lyric relations, and other business facts should bind to stable identity instead of filename alone.

## 6. Storage Pool and site types

The Storage Pool contains:

- Master Local;
- enabled Storage Followers.

Admin uploads select a **site type**, never a concrete Follower:

- `primary` -> Master Local / Local;
- `direct` -> eligible Direct Followers;
- `relay` -> eligible Relay Followers.

For Direct/Relay, placement considers all members of the requested type that are storage-enabled, online, writable, and large enough. Selection prefers lower current `(used + reserved) / allocated` pressure and then more available bytes.

Selection and durable upload reservation are serialized by the storage write lock so concurrent Admin sessions observe current reservations.

Existing media does not require ownership migration when presentation/site-type logic changes. Existing `storage_member_id`, `member_kind`, and `transport` remain authoritative.

## 7. Capacity semantics

FrontierCloud distinguishes physical filesystem facts from logical quota.

Operator presentation intentionally separates:

```text
physical used / physical total
FrontierCloud used / FrontierCloud allocated
```

`reserved_bytes` represents in-progress capacity commitments. `available_bytes` is a write-safety result, not a substitute for physical free space or logical allocation.

Master Local physical total/free are observed live from the media filesystem. Follower summaries report current physical facts through heartbeat/control state.

## 8. Managed-media mutation boundary

Filesystem paths and database metadata are one business-object lifecycle. Direct filesystem changes are not a supported substitute for managed mutations.

The process-local media mutation fence protects cross-cutting path changes. Current participants include:

- folder rename as the exclusive path mutation;
- upload-session reservation;
- global delete / pending-delete transitions;
- hide/unhide;
- directory/media priority changes.

Durable database facts such as upload reservations and `pending_delete` continue to act as fences after the short shared in-process lock ends.

### Single ASGI worker invariant

The mutation fence is process-local. Production Web therefore must run a **single ASGI worker**. Increasing `WEB_CONCURRENCY`, launching multiple Web workers, or horizontally scaling Web would create independent locks and invalidate the rename/upload/delete serialization model.

Multi-process Web requires a database/distributed mutation lock plus cross-process regression tests before deployment topology changes are allowed.

## 9. Folder rename

Folder rename is deliberately narrower than a general move API:

- supported managed `music` / `vido` folders only;
- same parent only;
- target collision rejected when real managed content exists;
- active uploads and pending deletes block rename;
- a Master coordinates every storage member containing that logical folder;
- required offline Followers block an unsafe rename;
- already-moved members are rolled back in reverse order if a later move/metadata commit fails;
- directory priority, visibility, media paths, playback facts, and lyric-link metadata move with the successful rename;
- a deleted name may be reused when only stale directory metadata remains.

## 10. Directory priority and visibility

Folders are first-class sorting objects. Public category/subcategory order is:

```text
directory priority descending -> name
```

Directory priority is distinct from media-file priority.

Visibility is inherited. Admin distinguishes **directly hidden** from **effectively hidden by an ancestor**, so a child does not offer a meaningless local unhide action while its ancestor still hides it.

## 11. Lyrics business boundary

Lyrics are Master-owned business content regardless of the audio object's storage member.

Supported hierarchy:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum. Upload, validation, Admin tree, scoped search, download/delete collection, catalog, and relation management must all enforce the same hierarchy.

`lyrics/default.lrc` is a runtime-maintained playback fallback. It is excluded from user content trees/counts/search and does not make a track appear to have a user-managed lyric relation.

Same-name auto-link is fallback automation: it may fill a missing/default relation, prefers the same relative album path, reports ambiguity, and must never overwrite an explicit user-selected business lyric.

A lyric upload succeeds only after both file publication and managed-object registration are durable together. Registration failure removes the new file. A later cache-invalidation error is fail-soft and must not erase committed content.

## 12. Catalog and cache consistency

Redis generation-based catalogs cache expensive scans/sorts. Mutable catalog HTTP surfaces revalidate so successful Admin mutations are visible promptly, while server-side generation caches prevent unnecessary filesystem/MySQL work.

Mutations that change paths, visibility, priority, or catalog membership invalidate the relevant generation. Cache invalidation is fail-soft **after** a durable business commit: a Redis failure is logged and TTL recovery remains available rather than turning a committed mutation into a false client failure.

## 13. Heartbeat and Backup separation

Heartbeat is node-health control traffic. Backup is asynchronous recovery work. They are separate failure domains.

A successful heartbeat remains successful even when a later Backup attempt fails. Backup must not block the heartbeat loop.

Backup artifacts are bounded Master business-recovery packages with chunking and integrity metadata. They are not online replicas, HA storage, or automatic failover.

Interrupted receiving generations are aborted/cleaned when supported so a failed transfer does not remain indefinitely as the apparent current recovery point.

## 14. Database schema generations

MySQL is the durable source for business and cluster facts. Existing initialized databases upgrade in place through explicit Schema Generations.

```text
frontiercloud_schema
frontiercloud_schema_migrations
```

Migration safety relies on:

- MySQL advisory lock;
- one-generation-at-a-time transitions;
- idempotent DDL helpers;
- generation marker advancing only after a generation succeeds;
- startup failing closed on migration failure or a database newer than the application.

## 15. Release architecture

Production follows `main`, but `main` HEAD is publishable only when provenance is proven from the accepted development path.

Required evidence:

1. current `main` HEAD is associated with the merged same-repository `dev -> main` PR;
2. the exact promoted `dev` SHA has a successful `push` CI run;
3. the reviewed dev tree equals the main tree.

GitHub merge/squash/rebase history identity may differ from the dev commit; tree equality is the code-content proof.

The Updater handles validation, image build, local replacement, Follower distribution, release status, rollback, and maintenance state.

## 16. Repository topology is architecture

The repository has exactly two canonical branches:

- `dev` — implementation and complete CI authority;
- `main` — reviewed release history.

Do not create feature/fix/release/temporary branches. The only PR into `main` is same-repository `dev -> main`. After merge, fast-forward `dev` to the new `main` merge commit before further work. Never force-rewrite either canonical branch.

## 17. Fail-closed boundaries

FrontierCloud refuses unsafe progress rather than guessing when:

- a fixed role loses required TLS/certificate trust;
- current GitHub release evidence cannot authorize a new target;
- a schema migration fails or the database is newer than the application;
- a required Follower is offline during a cross-member path mutation;
- protected media/recordings still belong to a node being revoked/reinitialized;
- cluster release convergence cannot be established safely.

The design goal is to preserve the already-running service through temporary external failures while refusing unverified new changes.
