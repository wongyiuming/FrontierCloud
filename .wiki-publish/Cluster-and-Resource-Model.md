# Cluster and Resource Model

## 1. Node roles

FrontierCloud nodes can be:

- `Standalone`
- `Master`
- `Follower`

### Standalone

The default single-node full-business mode. It owns its local business state and media without cluster relationships. HTTP is supported.

### Master

The single business authority in a cluster. It owns:

- public business APIs/pages;
- the global media catalog and placement truth;
- lyric files and lyric relations;
- playback and sorting business facts;
- karaoke user/recording business state;
- node desired configuration;
- release coordination;
- Admin audit state.

### Follower

A resource node paired to one Master. A Follower may independently provide:

- Storage;
- Backup;
- authenticated health/metrics;
- signed Direct/Relay data-plane operations;
- authenticated node-control operations.

Followers are not alternate business authorities.

### Compute Worker is retired

Compute Worker is no longer a product/runtime capability. Historical `cluster_compute_members` / `cluster_worker_jobs` data may remain for compatibility and backup safety, but Admin must not expose Compute configuration, the runtime must not lease Worker jobs, and new engineering work must not treat those tables as active product state.

## 2. Relationships

Relationships are stored in `node_relationships`.

Important fields include:

```text
relationship_id
peer_id
peer_endpoint
direction
mode
state
status
last_heartbeat
rtt_ms
failures
recoveries
peer_version
protocol
summary
```

From the Master, a Follower relation is downstream. From the Follower, the Master relation is upstream.

Fixed Master/Follower relationships require certificate-verified HTTPS.

## 3. Heartbeats

Heartbeats report and synchronize control-plane state such as:

- relation health;
- RTT;
- consecutive failures and recoveries;
- observed Storage state;
- observed Backup state;
- host/physical capacity observations;
- runtime summaries;
- peer release version/protocol information.

Heartbeat success is independent from Backup work. A Backup failure after a successful heartbeat does **not** retroactively mark the node offline. Backup executes asynchronously so it cannot block the next health loop.

## 4. Desired vs observed state

A setting accepted by the Master is desired state. It is not effective until the Follower confirms the observed state.

```text
Admin saves desired config
        |
        v
Master persists desired state
        |
        v
Follower receives authenticated control state
        |
        v
Follower reports observed state
        |
        v
Desired == Observed -> effective
```

If the Follower is offline, the correct UI meaning is "saved but not yet confirmed/effective".

Do not use an HTTP 200 from the configuration endpoint as proof that the remote Follower applied the change.

## 5. Storage members

Storage members are represented by `cluster_storage_members`.

Important persisted/derived facts include:

```text
member_id
relationship_id
member_kind
transport
storage_enabled
allocated_bytes
used_bytes
reserved_bytes
physical_free_bytes
health
writable
updated_at
```

Live summaries additionally expose current physical total capacity.

### Master Local

Master Local is the `primary` site type and uses Local transport.

### Follower Storage

A Follower Storage member uses Direct or Relay transport.

Direct/Relay mode switching is blocked while relevant upload reservations exist, because an in-flight upload must not change transport meaning halfway through its transaction.

## 6. Capacity model

Capacity has separate physical and logical meanings.

### Physical total / physical free

Real underlying filesystem facts. Master Local reads them live from the media filesystem. Followers report current physical observations through authenticated heartbeat/control state.

### allocated_bytes

Logical capacity FrontierCloud is allowed to use on that member.

### used_bytes

Current FrontierCloud project usage tracked for that member.

### reserved_bytes

Capacity durably reserved by in-progress uploads. It prevents concurrent sessions from all observing and consuming the same apparent free space.

### writable

Whether new placements are currently safe.

Admin intentionally shows:

```text
physical used / all
used / allocated
```

Do not collapse these into one percentage.

## 7. Upload placement by site type

The Admin upload selector starts empty. The user chooses a site type:

```text
primary
direct
relay
```

For `primary`, placement is Master Local.

For `direct` / `relay`, the user does not name a concrete member. FrontierCloud chooses among all members of the selected type that are:

- storage-enabled;
- online/healthy;
- writable;
- large enough logically and physically.

Placement prefers:

1. lower `(used + reserved) / allocated` pressure;
2. more available bytes as a tie-breaker.

Member selection plus durable reservation is serialized by the storage write lock so concurrent uploads see current `reserved_bytes`.

## 8. Storage transport vs business object

`Local`, `Direct`, and `Relay` are access/transfer choices, not different business objects.

The same global media object remains identified by its stable `media_id` and logical `media_path`; transport and storage owner determine how its bytes are reached.

Historical media does not need migration when the Admin site-type UI changes. Existing owner/member/transport fields remain source of truth.

## 9. Backup members

Backup configuration is independent from Storage configuration.

A Backup Follower stores Master business-recovery artifacts, not live MySQL replicas.

Important operator facts include:

- desired enabled state;
- observed/effective state;
- last successful backup;
- latest attempt/result;
- recovery-point size;
- checksum/integrity result;
- generation/recovery-point identity;
- recovery-point count.

Concrete recovery data uses business-backup metadata/chunk tables.

## 10. Backup transfer model

Backup construction and transfer are bounded and chunked. Large business state is not treated as one unbounded in-memory HTTP message.

The internal Backup route has a dedicated Nginx request-size boundary large enough for the encoded application chunk. Control requests use a longer timeout than ordinary small control operations.

Interrupted receiving generations are best-effort aborted/cleaned before or after failed attempts where supported so a stale `receiving` state does not live forever.

Backup failure does not make heartbeat unhealthy.

## 11. What Backup is not

Backup is not:

- MySQL Group Replication;
- live SQL primary/replica;
- automatic Master election;
- automatic failover;
- an online replica of every media object.

It is a verifiable business-recovery artifact.

## 12. Protected node operations

Revoke and reinitialize are destructive control actions.

FrontierCloud rejects them when the node still owns protected resources such as:

- active media placements;
- pending-delete media;
- recordings or other durable protected objects.

Resolve ownership through supported application workflows first. Do not remove database rows or bytes manually to bypass this protection.

Storage allocation also cannot be reduced below current protected use.

## 13. Resource health and release health are separate

A node can be healthy for heartbeat/Storage/Backup but still run an older release. Conversely, every node can be on the same SHA while the most recent Backup attempt failed.

Admin intentionally keeps these dimensions separate.

A useful health evaluation order is:

```text
1. Relationship active
2. Heartbeat recent
3. RTT/failure counters reasonable
4. Desired == Observed for configured resources
5. Storage/Backup execution facts healthy
6. Node release SHA matches intended cluster target
```

## 14. Cluster release convergence

A successful Master replacement does not prove cluster convergence.

For each Follower verify:

```text
reachable == true
release_branch == main
current_sha == target_sha
release state compatible with successful completion
```

Maintenance mode may intentionally remain active while the cluster is on mixed versions or release convergence failed. Do not force it off before understanding the version split.
