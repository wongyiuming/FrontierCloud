# Cluster and Resource Model

## 1. Node roles

FrontierCloud nodes may be:

- `Standalone`
- `Master`
- `Follower`

### Standalone

The default state before a node is fixed into a production cluster role.

### Master

The single business authority. It owns:

- business APIs;
- the global media catalog;
- node configuration authority;
- Storage/Compute/Backup scheduling facts;
- release coordination;
- business database and audit state.

### Follower

A resource node that may independently enable:

- Storage;
- Compute;
- Backup.

The capabilities are independent and do not have to be enabled together.

## 2. Relationships

Node relationships are stored in `node_relationships`.

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

From the Master perspective, a Follower relation is normally downstream. From the Follower perspective, the Master relation is upstream.

## 3. Heartbeats

Heartbeats synchronize and report:

- online state;
- RTT;
- consecutive failures;
- recovery count;
- observed Storage state;
- observed Compute state;
- observed Backup state;
- host resource metrics;
- runtime summaries.

Admin UI values such as current/average/minimum/maximum RTT are meant to distinguish one recent measurement from a time-window view.

## 4. Desired vs observed state

A setting stored by the Master is desired state.

For example:

```text
Compute = enabled
worker_slots = 4
```

means only that the Master accepted and stored the configuration.

The configuration becomes effective after the Follower receives it and reports the observed state through control heartbeats.

```text
configuration saved
       |
       v
waiting for Follower confirmation
       |
       v
Desired == Observed
       |
       v
effective
```

If the Follower is offline, the correct state is "saved but not yet confirmed/effective". HTTP success from the configuration endpoint is not proof that the remote node applied it.

## 5. Storage

Storage member table:

```text
cluster_storage_members
```

Important fields:

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

### allocated_bytes

Logical capacity that FrontierCloud is allowed to use.

### used_bytes

Reported capacity already consumed.

### reserved_bytes

Capacity reserved by in-progress operations but not necessarily represented by active catalog objects yet.

### physical_free_bytes

Actual free capacity from the underlying filesystem.

### writable

Whether new placements are currently allowed.

Placement decisions must respect health, writable state, logical availability, reservations, and physical free space.

## 6. Compute

Compute member table:

```text
cluster_compute_members
```

Important fields:

```text
member_id
enabled
worker_slots
available_slots
cpu_percent
memory_available_bytes
capabilities
updated_at
```

### worker_slots

Maximum concurrent Worker tasks allowed on the Follower.

Example:

```text
worker_slots = 4
running = 2
```

means two slots are still available in principle.

### Worker jobs

Jobs are stored in:

```text
cluster_worker_jobs
```

with fields such as:

```text
job_id
job_type
media_id
member_id
payload
result
state
lease_token_hash
lease_expires_at
attempts
created_at
updated_at
```

Current job types include work such as media hashing, probing, and metadata-related tasks.

### Why a job was assigned to a Follower

Two primary reasons are recorded:

**Pinned**  
The job explicitly identifies a member and is only eligible for that Follower.

**Capability + FIFO**  
The job is shared. Capable Followers compete for the lease, and queue order determines which job is offered first.

This is not currently a Kubernetes/Nomad-style CPU/memory scoring scheduler. CPU and memory are observable, but ordinary placement is not described as a weighted scorer.

## 7. Worker lease semantics

Jobs are leased rather than simply popped from a queue.

```text
queued
  |
  v
leased / running
  |
  v
completed
```

If a worker disappears, a lease may expire and the job can be retried. `attempts` helps expose repeated execution attempts.

## 8. Backup

Backup member table:

```text
cluster_backup_members
```

Important fields:

```text
member_id
enabled
generation
last_success
lag_seconds
checksum
state
updated_at
```

Concrete recovery-point metadata and chunks are stored in:

```text
cluster_business_backups
cluster_business_backup_chunks
```

Operator-facing Backup state should emphasize:

- enabled and effective status;
- last successful backup time;
- most recent attempt/result;
- latest recovery-point size;
- checksum;
- recovery-point count;
- next planned attempt.

### generation

The generation is primarily a machine identity for a recovery point. It belongs in technical detail, while operators usually need last-success time and result first.

### What Backup is not

Backup is not:

- MySQL Group Replication;
- live primary/replica SQL replication;
- automatic Master election;
- automatic failover.

It is a verifiable business recovery artifact.

## 9. Evaluating node health

Use this order rather than looking at a single green toggle:

```text
1. Relationship is active
2. Heartbeat is recent
3. RTT/failure counters are reasonable
4. Desired equals Observed
5. Storage/Compute/Backup execution results are healthy
6. Node release version matches the intended cluster target
```

## 10. Revoke and reinitialize

These are destructive control operations.

If a node still owns active media, pending-delete data, recordings, or other protected resources, FrontierCloud should reject revoke/reinitialize rather than leave catalog references pointing to a missing node.

Move or remove protected placements through supported workflows before revoking a relationship.

## 11. Release state is a separate dimension

A Follower may have healthy Storage, Compute, and Backup but still run an older release. Conversely, every node may be on the same SHA while Backup is currently failing.

The Admin UI intentionally keeps resource health and release convergence separate.
