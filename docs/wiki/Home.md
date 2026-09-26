# FrontierCloud Wiki

FrontierCloud is a self-hosted media browsing, playback, karaoke, resource-node, and administration system. FastAPI provides the business and control plane, the browser UI uses native JavaScript, and Docker Compose runs the Web/API, Nginx, MySQL, Redis, Updater, and STUN services.

This Wiki is written for two audiences:

- **Operators**: deployment, node onboarding, storage distribution, Compute, Backup, releases, rollback, and troubleshooting.
- **Engineers**: data models, cluster boundaries, schema migrations, release rules, CI, and code-level constraints.

## Documentation map

- [Architecture](Architecture.md)
- [Deployment and Configuration](Deployment-and-Configuration.md)
- [Cluster and Resource Model](Cluster-and-Resource-Model.md)
- [Media Catalog and Storage Placement](Media-and-Storage.md)
- [Operations and Troubleshooting](Operations-and-Troubleshooting.md)
- [Release, Rollback, and Database Migrations](Release-and-Database-Migrations.md)
- [Engineering and CI](Engineering-and-CI.md)
- [Bash / SQL Command Reference](Command-Reference.md)

## Core principles

### The Master is the only business authority

A FrontierCloud cluster has one business Master. The Master owns the global media catalog, business database, lyric relationships, playback facts, users, audits, and cluster-control state.

Followers do not own an independent business catalog. A Follower is a resource node and may independently provide Storage, Compute, Backup, or any combination of them.

### The global media catalog is authoritative

The relation between a media path and its physical storage node is stored by the Master rather than inferred by scanning file systems.

```text
global_media_objects.storage_member_id
                |
                v
cluster_storage_members.member_id
                |
                +-- relationship_id --> node_relationships
```

For every managed media object, the system can answer:

- its stable `media_id`;
- its logical path;
- which Storage Member owns the placement;
- which Follower that member represents;
- the current storage-node health;
- whether access is Local, Direct, or Relay.

### Desired state and observed state are different

A resource toggle saved on the Master is only desired state. It is considered effective only after the Follower receives and reports the applied state through the control heartbeat. Storage, Compute, and Backup UI/API semantics must preserve this distinction.

### Releases fail closed

A new release must pass release verification. If GitHub or CI verification is temporarily unavailable, the running service continues, but a new upgrade is not authorized.

### Existing databases upgrade in place

FrontierCloud uses versioned Schema Generations. Existing initialized databases advance through explicit migrations instead of requiring an empty database whenever a table or column changes.

## Main runtime components

| Component | Purpose |
| --- | --- |
| Nginx | HTTP/HTTPS entry point, static resources, proxying, maintenance mode, and edge enforcement |
| Web | FastAPI business API, Admin, cluster control, catalog, and media logic |
| MySQL | Business facts, global catalog, node relationships, jobs, audits, migration history |
| Redis | Runtime cache and coordination data |
| Updater | Local build/replace, upgrade, rollback, and cluster release coordination |
| STUN / Coturn | STUN service required by WebRTC observation |

## Important terms

**Standalone**  
A node that has not been fixed as a Master or Follower.

**Master**  
The single business node and cluster-control authority.

**Follower**  
A resource node. It is not a second public business site.

**Storage Member**  
A member that may hold media objects, including Master Local and enabled Storage Followers.

**Compute Slot**  
The maximum number of Worker tasks that a Follower may execute concurrently.

**Backup Member**  
A Follower that stores Master business recovery artifacts. Backup is not an online database replica or automatic failover mechanism.

**Generation**  
The formal version of the database schema.

**Placement**  
The relation between a media object and the Storage Member that physically owns it.

## Where to start

For deployment, read [Deployment and Configuration](Deployment-and-Configuration.md).

To answer "which node actually stores this file?", read [Media Catalog and Storage Placement](Media-and-Storage.md).

For node health, Compute, and Backup, read [Cluster and Resource Model](Cluster-and-Resource-Model.md).

Before a release, read [Release, Rollback, and Database Migrations](Release-and-Database-Migrations.md).

For incidents, start with [Operations and Troubleshooting](Operations-and-Troubleshooting.md) and [Bash / SQL Command Reference](Command-Reference.md).
