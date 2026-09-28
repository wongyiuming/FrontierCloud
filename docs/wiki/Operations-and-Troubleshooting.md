# Operations and Troubleshooting

## 1. Diagnose by layer

Do not begin an incident by restarting every container. Preserve evidence and separate independent failure domains.

Recommended order:

```text
1. Is public/business traffic actually unavailable?
2. Nginx / Web liveness and readiness
3. MySQL / Redis
4. Master/Follower relationship and heartbeat
5. Storage / Backup state
6. Managed-media mutation/recovery state
7. Updater / release convergence
8. External dependencies such as GitHub
```

A GitHub release-verification problem does not imply playback failure. A Backup failure does not imply heartbeat or Storage failure.

## 2. Fast host check

```bash
docker compose ps
```

```bash
curl -fsS http://127.0.0.1/health/live && echo
curl -fsS http://127.0.0.1/health/ready && echo
```

Use the real HTTPS hostname for a production fixed-role node.

Recent logs:

```bash
docker compose logs --tail=300 web
docker compose logs --tail=300 nginx
docker compose logs --tail=300 updater
docker compose logs --tail=300 mysql
docker compose logs --tail=300 redis
```

## 3. Capture evidence before changing state

```bash
date -Is
docker compose ps
git rev-parse HEAD
git branch --show-current
git status --short
```

Save key logs before applying a fix:

```bash
docker compose logs --tail=500 web > /tmp/frontier-web.log
docker compose logs --tail=500 updater > /tmp/frontier-updater.log
docker compose logs --tail=500 nginx > /tmp/frontier-nginx.log
```

## 4. Fixed-role TLS problems

Symptoms can include startup refusal, pairing/control failures, or repeated relationship failure while Standalone HTTP would otherwise work.

Check:

- `TLS_ENABLED=true`;
- `SERVER_NAME` matches the certificate;
- certificate and key paths exist;
- DNS resolves to the intended endpoint;
- the certificate chain is valid from the peer;
- no proxy/firewall is replacing or breaking TLS.

Fixed Master/Follower roles intentionally fail closed. Do not work around a certificate problem by downgrading the cluster control plane to HTTP.

## 5. Follower appears Offline

Inspect:

1. relationship state;
2. `last_heartbeat` freshness;
3. RTT;
4. consecutive failure/recovery counters;
5. peer endpoint/DNS/routing/firewall;
6. Follower Web readiness;
7. certificate trust;
8. peer version/protocol summary.

One high RTT sample is not enough to classify a node as offline. Heartbeat freshness and repeated failures matter more.

## 6. Desired is saved but not effective

A resource configuration endpoint returning success means the Master persisted desired state. It does not prove the Follower applied it.

If Desired != Observed, check:

- Follower online state;
- the next authenticated control heartbeat;
- relationship/TLS validity;
- peer version/protocol compatibility;
- whether the requested transition is blocked by a durable safety condition.

For example, Direct/Relay transport switching can be blocked while upload reservations exist.

## 7. Storage capacity looks wrong

Use the correct dimensions:

```text
physical used / physical total
FrontierCloud used / allocated
reserved bytes
health / writable
```

Do not interpret logical allocation as filesystem free space.

Master Local physical facts are observed live from the media filesystem. Follower physical observations come from current heartbeat/control summaries.

## 8. Upload rejected despite apparent free space

Possible causes:

- site type was not explicitly selected;
- no member of the selected `direct` / `relay` type is currently ready;
- logical allocation is exhausted;
- `reserved_bytes` consume the remaining logical space;
- physical free space is insufficient;
- the member is unhealthy/offline;
- `writable=false`;
- a path lease already exists;
- an old reserved upload has not yet converged/expired;
- the same logical path is still `pending_delete`.

The Admin user selects a **site type**, not a concrete Follower. If one Direct member is full but another eligible Direct member is ready, placement can choose the other member automatically.

## 9. Old upload blocks a path

Completed upload sessions should not keep a path lease. Expired reserved sessions are cleanup candidates. Pending remote storage cleanup may still block immediate reuse.

Do not manually delete uniqueness/path-locator rows unless performing deliberate data recovery. Let the upload reconciliation and cleanup path converge first.

## 10. Folder rename fails

Controlled folder rename is intentionally strict.

Check:

- source path is a supported real `music` / `vido` folder;
- the new name stays under the same parent;
- no active upload exists in source/target scope;
- no affected media remains pending deletion;
- no real target media/path collision exists;
- every required storage member is reachable;
- no concurrent exclusive mutation is active.

A previously deleted name is allowed to be reused if only stale directory metadata remains. If rename still reports a collision, verify there is not real active media, a pending delete, an upload reservation, or a physical target directory.

Cross-member rename rollback is reverse-order. If a later member or metadata commit fails, already moved members should be moved back.

## 11. Managed delete appears stuck

Managed delete is not `rm`.

Inspect:

- `global_media_objects.state`;
- pending-delete rows;
- storage member health/relationship;
- remote delete retry logs;
- local recovery/quarantine state;
- whether the remote object is already missing and only metadata convergence remains.

Do not use direct `rm -rf` to clear pending-delete or recovery state. That can make the catalog and filesystem diverge further.

## 12. Visibility looks inconsistent

An item can be directly hidden or effectively hidden by an ancestor.

If Admin says an item is hidden by its parent, unhiding the child alone cannot make it public. Change the ancestor visibility through the supported Admin path.

If the public catalog appears stale after a successful visibility/path/priority mutation, first force browser revalidation/cache refresh; the server-side Redis catalog uses generation invalidation and TTL recovery.

## 13. Directory priority does not appear to apply

Public category/subcategory order is:

```text
directory priority descending -> name
```

Directory priority is separate from file/media priority.

After a successful priority update, the relevant catalog generation should be invalidated. A cache hit should not trigger a fresh MySQL sort query.

## 14. Lyrics folder is missing from Admin

Supported hierarchy is exactly:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Anything deeper is outside the supported managed hierarchy.

Accepted upload should register the lyric as a managed object before the API reports success. If a file was manually copied into `data/media/lyrics`, do not assume it is equivalent to a managed upload.

## 15. `lyrics/default.lrc` behavior

`lyrics/default.lrc` is internal fallback content.

Expected behavior:

- playback may use it when no user lyric is selected;
- Admin lyric trees/search/counts do not show/count it;
- a fallback relation does not make a track appear manually linked;
- it is generated/repaired at runtime;
- it should not be Git-tracked under `data/`.

If the file is missing, runtime code should recreate/repair it rather than requiring a repository checkout to carry it.

## 16. Same-name lyric auto-link did not link a track

Auto-link is intentionally conservative.

Check:

- music and lyric stems match exactly;
- paths stay within supported hierarchy;
- more than one lyric candidate did not create ambiguity;
- the track does not already have an explicit non-default user relation.

Auto-link must not overwrite an explicit user choice, even if that lyric file is temporarily unavailable.

## 17. Lyric relation folder counts seem wrong

The left lyric and right music directory entries show recursive supported-file counts.

- lyric side excludes `lyrics/default.lrc`;
- music side counts supported audio files within the relation-browser scope;
- unsupported/deeper hierarchy or hidden internal fallback content should not inflate the user count.

## 18. Backup failed but node is still Online

That can be correct.

Heartbeat and Backup are independent failure domains. A successful heartbeat remains successful even when an asynchronous Backup attempt later fails.

Inspect Backup separately:

- enabled/effective state;
- last successful recovery point;
- latest attempt/result;
- checksum;
- size/chunk count;
- current receiving/failed/complete state;
- Web/Nginx logs for `/internal/v1/backup/`.

## 19. Backup stuck in `receiving`

Historical builds could fail after `backup/begin` if encoded chunk requests exceeded the generic Nginx request-size limit.

Current builds use a dedicated internal Backup location sized for the application's bounded chunk envelope and a longer control timeout.

For a stale generation:

- verify both nodes are current enough to support abort cleanup;
- inspect the latest attempt and chunk state;
- allow the next attempt to best-effort abort/clean stale receiving state;
- do not delete chunk rows blindly unless doing deliberate disaster recovery.

## 20. GitHub release verification is rate-limited

Business traffic can remain healthy while release verification is unavailable.

When GitHub REST returns 403/429 with exhausted quota:

- current running services stay up;
- existing cluster status remains observable;
- new upgrade authorization fails closed;
- rollback may still rely on local updater history;
- the verifier enters backoff instead of repeatedly hammering GitHub;
- last-known-good evidence is display-only.

A production Master can use a least-privilege read-only token:

```dotenv
GITHUB_API_TOKEN=...
```

## 21. System Release Management version display

The version summary is semantic rather than always showing `current -> target`.

Converged example:

```text
<sha> · 已与 main HEAD 一致
```

Pending upgrade example:

```text
当前 <sha> -> 待发布 <sha>
```

If both SHAs are the same, an arrow transition should not be interpreted as meaningful deployment work.

## 22. Cluster versions differ

A successful Master replacement does not prove all Followers converged.

For each Follower verify:

```text
reachable == true
release_branch == main
current_sha == target_sha
state compatible with successful completion
```

Mixed versions may intentionally keep maintenance enabled. Do not turn maintenance off first and investigate later.

## 23. Updater is stuck or failed

```bash
docker compose logs --tail=500 updater
```

Inspect:

```text
state
phase
target_sha
current_sha
previous_sha
detail
```

Typical phases include validation, build, replacement, distribution, and complete.

Rollback uses the previous Web-managed SHA; it is not an automatic database-schema downgrade.

## 24. MySQL migration problems

### Database generation behind application

The app should migrate one registered generation at a time.

### Migration fails

Startup fails closed and the generation marker does not advance. Fix the root cause and restart. Migrations are designed to be idempotent/resumable.

### Database newer than application

Startup is rejected. FrontierCloud does not automatically downgrade schema.

### Non-empty database has no FrontierCloud marker

Do not guess an unknown historical schema. Determine the origin/recovery point before attempting adoption.

## 25. Browser UI still shows old behavior

FrontierCloud uses content-hashed/static integrity URLs for many Admin modules, but browser/runtime caches can still matter during aggressive iteration.

Rapidly click the first half of the home logo five times to force the project UI cache refresh path, or use a clean browser session when validating a newly deployed UI.

Always verify the running cluster SHA before concluding that a code fix did not deploy.

## 26. Recovery asset set

At minimum, production recovery should preserve together:

```text
MySQL
./data
runtime_secrets
```

Restoring only one component can create catalog/filesystem/credential drift.

## 27. Commands that are not routine fixes

Do not use these casually:

```bash
docker compose down --volumes
rm -rf data/*
docker system prune -a --volumes
```

Do not directly `mv`/`rm` managed media, mutate Schema Generation, or force-rewrite `dev`/`main` as a troubleshooting shortcut.
