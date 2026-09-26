# Architecture

## 1. System overview

FrontierCloud uses a single business Master with multiple resource Followers.

```text
                    +----------------------+
                    |       Browser        |
                    +----------+-----------+
                               | HTTP/HTTPS
                               v
                    +----------------------+
                    |        Nginx         |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    |   FastAPI / Web      |
                    |       Master         |
                    +------+-------+-------+
                           |       |
                   MySQL / Redis   | signed cluster control
                           |       |
                           v       v
                    +---------+  +------------------+
                    |Business |  | Follower nodes   |
                    |facts    |  | Storage/Compute  |
                    |catalog  |  | Backup           |
                    +---------+  +------------------+
```

The frontend is native HTML/CSS/JavaScript rather than a React/Vue SPA. Media playback uses browser media APIs, and karaoke integrates the existing Rust/WASM module with browser audio capabilities.

## 2. Control plane and data plane

### Control plane

The control plane owns:

- node identity and role;
- Master/Follower pairing;
- heartbeat and state synchronization;
- Storage/Compute/Backup configuration;
- release upgrade and rollback;
- Admin operations and audit;
- database Schema Generation.

Important tables include:

```text
node_identity
node_relationships
node_audit
cluster_storage_members
cluster_compute_members
cluster_backup_members
cluster_worker_jobs
```

### Data plane

The data plane handles:

- media upload;
- playback and download;
- Direct / Relay transfer;
- Follower-local media-object access;
- Compute Worker tasks;
- transfer of Master business recovery artifacts to Backup Followers.

Control-plane configuration and data-plane results must not be conflated. For example, `Backup enabled` is configuration; `last successful backup` is an execution result.

## 3. Data ownership

### Master-owned facts

The Master is authoritative for:

- the global media catalog;
- media paths and stable media IDs;
- lyrics and lyric links;
- playback scores and preferences;
- users and Admin business state;
- relationships and audits;
- Compute job facts;
- Backup metadata;
- Schema Generation and migration history.

### Follower-local resources

A Follower may store:

- media objects placed on that node by the Master;
- local Compute runtime state;
- Master business recovery artifacts;
- machine metrics reported in heartbeats.

A Follower must not create a second independent business catalog.

## 4. Media identity

A managed media object includes these concepts:

- `media_id`: stable global business identity;
- `media_path`: logical business path;
- `storage_member_id`: current physical placement;
- `object_id`: object identity within the storage member;
- `path_locator`: path uniqueness locator;
- `state`: object lifecycle state such as `active` or `pending_delete`.

`global_media_objects` is the authoritative placement table.

## 5. Storage Pool

The Storage Pool consists of:

- Master Local;
- all Followers with Storage enabled.

`cluster_storage_members` records:

- allocated capacity;
- used capacity;
- reserved capacity;
- physical free space;
- health;
- writable state;
- transport;
- relationship ID.

An upload may only target a writable member that satisfies health, logical-capacity, reservation, and physical-space requirements. One logical path maps to one complete object on one member; FrontierCloud is not a cross-node chunking filesystem.

## 6. Compute Pool

`cluster_compute_members` records:

- enabled state;
- `worker_slots`;
- `available_slots`;
- CPU percentage;
- available memory;
- capabilities.

Worker jobs are stored in `cluster_worker_jobs`.

Current placement reasons are primarily:

1. **Pinned**: the job explicitly specifies a member;
2. **Capability + FIFO**: an unpinned job is leased by the first available capable Follower according to queue order.

Slots represent real concurrent Worker capacity, not a decorative configuration number.

## 7. Backup

A Backup Follower receives a Master-generated business recovery artifact, not a live MySQL physical replica.

The flow provides:

- chunked transfer;
- SHA-256 integrity verification;
- generation-based recovery-point identity;
- last-success tracking;
- recovery-point count;
- last-attempt status;
- shorter retry after failure than the normal backup interval.

Backup is disaster-recovery storage, not automatic failover.

## 8. Network and transport

Fixed Master/Follower roles require certificate-verified HTTPS.

Media access may resolve to:

- **Local**: the resource is on Master Local;
- **Direct**: the client/request path accesses the resource Follower directly;
- **Relay**: data moves through the controlled relay path.

Public business pages terminate at the Master. A Follower is not an independent public business application.

## 9. Database

MySQL is the primary durable store for business and cluster facts.

Redis is runtime cache/coordination and must not be the sole store for facts that need durable recovery.

Production databases store their schema version in `frontiercloud_schema` and migration history in `frontiercloud_schema_migrations`.

## 10. Updater and release architecture

The Updater is a separate control component responsible for:

- validating/checking out target SHAs;
- building Web/Nginx release images;
- replacing local containers;
- driving Follower upgrades;
- reporting release state;
- rollback;
- maintenance state.

Production follows `main`, but the current `main` target is publishable only when it can be proven to come from an accepted `dev -> main` PR, the exact PR head has successful dev push CI, and the reviewed dev tree equals the main tree.

## 11. Fail-closed boundaries

FrontierCloud rejects unsafe progress rather than guessing when:

- a fixed node role lacks required TLS/certificate conditions;
- GitHub/CI cannot verify a new release target;
- the database belongs to a newer Schema Generation than the application;
- a schema migration fails;
- a Follower has not confirmed desired resource configuration;
- a node still owns protected media/recordings during revoke or reinitialize;
- cluster release convergence cannot be established.

The operational goal is to keep existing service available through temporary external failures while refusing unverified new changes.
