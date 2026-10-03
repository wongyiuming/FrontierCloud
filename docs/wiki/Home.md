# FrontierCloud Wiki

FrontierCloud is a self-hosted media browsing, playback, karaoke, cluster-storage,
backup and administration system. Native Go (Gin) with SQLite is the default;
an explicit FastAPI reference profile and optional MySQL support mixed clusters.
Browser JavaScript, Nginx, Redis and STUN keep their established roles. Native
Web, initialization, maintenance and the default updater do not require Python.

This Wiki documents the **current implementation**, not aspirational architecture. The project has changed quickly, so treat repository code, `ARCHITECTURE.md`, `CONTRIBUTING.md`, and exact-SHA CI as the final authority when a page and code disagree.

## Documentation map

- [Architecture](Architecture)
- [Deployment and Configuration](Deployment-and-Configuration)
- [Cluster and Resource Model](Cluster-and-Resource-Model)
- [Media Catalog and Storage Placement](Media-and-Storage)
- [Operations and Troubleshooting](Operations-and-Troubleshooting)
- [Release, Rollback, and Database Migrations](Release-and-Database-Migrations)
- [Engineering and CI](Engineering-and-CI)
- [Audit and Validation](Audit-and-Validation)
- [Bash / SQL Command Reference](Command-Reference)

## Current architecture in one page

### One business Master

A FrontierCloud cluster has one business Master. The Master owns:

- the global media catalog;
- lyrics and lyric relations;
- playback facts and sorting preferences;
- karaoke user/recording business state;
- Admin audit facts;
- node configuration authority;
- release coordination.

Followers are resource nodes. They may provide Storage and/or Backup plus authenticated health/control/data-plane services. They are not secondary business sites.

**Compute Worker is retired.** Historical compatibility tables may remain, but Worker slots, scheduling, job leasing, and product UI are not active features.

### Standalone remains a full single-node mode

A fresh node starts as Standalone. HTTP remains valid for Standalone. Fixing a node as Master or Follower requires certificate-verified HTTPS; a fixed role fails closed if its TLS requirements later disappear.

### The global media catalog is authoritative

The Master records each managed media object's stable identity, logical path, current storage owner, object identity, lifecycle state, and transport. Ownership is not inferred by scanning every Follower filesystem.

```text
global_media_objects.storage_member_id
                |
                v
cluster_storage_members.member_id
                |
                +-- relationship_id --> node_relationships
```

One logical path identifies one complete object on one storage member. FrontierCloud is not a cross-node chunking filesystem.

### Uploads choose a site type, not a concrete Follower

The Admin upload selector starts empty. The operator chooses:

- `primary` — Master Local / Local transport;
- `direct` — an eligible Direct Follower;
- `relay` — an eligible Relay Follower.

For Direct/Relay, FrontierCloud chooses among ready members of that type. Selection prefers lower `(used + reserved) / allocated` pressure and then more available bytes, while respecting health, writable state, logical allocation, reservations, and real physical capacity.

### Managed path mutations are transactional

Folder rename, upload reservation, delete, hide/unhide, and priority changes participate in one mutation-fence model. Folder rename is deliberately narrow: same parent only, real `music`/`vido` directories only, full metadata update, cross-member rollback on failure, and no guessing around active uploads/pending deletes/offline required Followers.

Web remains one process: one native Go service or one Python ASGI worker.
Native shared-volume mutation/recovery also retains OS leases; this does not
authorize horizontal Web replicas or additional business authorities.

### Lyrics are Master-owned business content

Supported hierarchy:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum. Upload, validation, Admin tree/search, download/delete collection, catalog, and relation management enforce the same boundary.

`lyrics/default.lrc` is an internal playback fallback. It is generated/repaired at runtime and excluded from user-visible Admin trees, search, counts, and business lyric-relation counts.

Folder upload preserves supported relative structure. Same-name auto-link matches exact stems, prefers the same relative album path, reports ambiguity rather than guessing, and never overwrites an explicit user-managed lyric relation.

### Backup is recovery, not HA

Backup is asynchronous and independent from heartbeat health. A failed Backup attempt does not turn a successful heartbeat into an offline node. Backup stores bounded, chunked Master business-recovery artifacts with integrity metadata on enabled Backup Followers.

Backup is not live SQL replication, HA storage, automatic Master election, or automatic failover.

### Releases fail closed

The owner-authorized runtime profiles are:

```text
gin_dev / gin_main  native Go implementation / reviewed release
dev / main          Python reference implementation / reviewed release
```

No additional feature/fix/release/temporary branches are allowed. Only same-repository
`dev -> main` and `gin_dev -> gin_main` promotions are valid. After merge, synchronize
that profile's implementation branch with its release merge commit.

Either profile is publishable only with exact merged-promotion provenance,
successful newest source push CI and an identical reviewed tree. A whole mixed
release requires independent proof for both artifacts; peers privately select
their own runtime profile. Temporary verification failure disables upgrade
authorization without taking down the running service.

## Current Admin domains

The final Admin module order is intentional and regression-tested:

1. Media management
2. Media sorting policy
3. Lyric relations
4. Karaoke users
5. IP security
6. WebRTC network relations
7. Nodes
8. Site access state
9. System release management
10. Admin Key
11. Brand Logo

## Important terms

**Standalone**

Single-node full-business mode before a fixed cluster role is chosen.

**Master**

The single business authority and cluster-control authority.

**Follower**

A resource node paired to one Master.

**Storage Member**
Master Local or an enabled Storage Follower that may own complete media objects.

**Placement**
The authoritative relation between a logical media object and the storage member that owns its bytes.

**Site Type**
Admin upload intent: `primary`, `direct`, or `relay`. It is not a concrete member ID.

**Desired / Observed**
Saved configuration versus state actually confirmed by a Follower.

**Backup Recovery Point**
A bounded Master business-recovery artifact, not an online replica.

**Schema Generation**
The formal version of the FrontierCloud MySQL schema.

## Where to start

- New deployment: [Deployment and Configuration](Deployment-and-Configuration)
- Roles, heartbeat, Storage, Backup: [Cluster and Resource Model](Cluster-and-Resource-Model)
- Media placement, upload, rename, delete, lyrics: [Media Catalog and Storage Placement](Media-and-Storage)
- Incident diagnosis: [Operations and Troubleshooting](Operations-and-Troubleshooting)
- Releases and migrations: [Release, Rollback, and Database Migrations](Release-and-Database-Migrations)
- Contribution rules and CI: [Engineering and CI](Engineering-and-CI)
- Ready-to-run diagnostics: [Bash / SQL Command Reference](Command-Reference)
