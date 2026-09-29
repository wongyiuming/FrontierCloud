# FrontierCloud

FrontierCloud is a self-hosted media browsing, playback, karaoke, cluster-storage, backup, and administration system. FastAPI provides the business/control plane, native browser JavaScript provides the UI, and Docker Compose runs Web/API, Nginx, MySQL, Redis, Updater, and the required WebRTC STUN service.

The current design is intentionally strict: a cluster has one business Master, Followers are resource nodes rather than secondary business sites, managed media mutations are transactional, and releases come only from the reviewed `dev -> main` path.

- [Project Wiki](https://github.com/wongyiuming/FrontierCloud/wiki)
- [Architecture boundaries](ARCHITECTURE.md)
- [Engineering / Git rules](CONTRIBUTING.md)
- [2026-09 four-area audit and validation](docs/audits/2026-09-27.md)

Read `ARCHITECTURE.md` and `CONTRIBUTING.md` before changing cross-cutting behavior.

## Quick start

HTTP needs no `.env` file:

```bash
docker compose up -d --build --wait
```

Open `http://localhost`. Media can be uploaded through Admin, or initialized under:

```text
data/media/music
data/media/vido
data/media/lyrics
```

The startup initializer creates the managed data tree and grants the unprivileged Web process the required access.

Initialization also creates runtime secrets in the persistent `runtime_secrets` volume. Read the current Admin Key or metrics token with:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

MySQL credentials are stored as `mysql_password` and `mysql_root_password` in the same directory. Restarts do not rotate these values. Startup logs list newly created secret names without printing values.

Rapidly click the second half of the home logo five times to enter the Admin Key. Rapidly click the first half five times to force a UI cache refresh. Admin can replace the long-term key, generate a new random key, or issue a single-use temporary key with a 15/30/60/120 minute sliding session. Persistent Admin sessions default to 180 minutes of inactivity.

## HTTPS and configuration

Create `.env`:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

Provide a certificate at `certs/fullchain.pem` and private key at `certs/privkey.pem`, or use the supported path overrides in `.env.example`.

Fixed Master/Follower roles require certificate-verified HTTPS. HTTP remains valid for Standalone operation; a fixed cluster role fails closed rather than silently downgrading when its TLS requirements disappear.

Public ports bind IPv4 by default. Set `PUBLIC_BIND_ADDRESS=::` only when the host and network are intentionally ready to publish IPv6.

`.env.example` is the supported configuration contract. Runtime secrets are generated internally rather than supplied through `.env`.

## Architecture snapshot

### Roles and authority

- **Standalone** is the default single-node full-business mode.
- **Master** is the only business authority in a cluster. It owns the global media catalog, lyrics and lyric relations, playback facts, karaoke users/recordings business state, Admin audit facts, node configuration, and release coordination.
- **Follower** is a resource node paired to one Master. It can provide Storage and/or Backup plus authenticated health/control/data-plane services. It is not a second public business site.
- **Compute Worker is retired.** Historical compatibility tables may still exist, but Worker slots, scheduling, and product UI are not active capabilities.

Public business pages and business APIs terminate at the Master. Fixed roles use certificate-verified HTTPS between nodes.

### One logical media object, one placement

The Master global catalog records each managed media object's stable identity, logical path, current storage owner, object identity, lifecycle state, and transfer mode. One logical path identifies one complete object on one member; FrontierCloud is not a cross-node chunking filesystem and does not infer ownership by scanning every Follower.

The Storage Pool contains Master Local plus enabled Storage Followers. Playback/download resolve the placement transparently through **Local**, **Direct**, or **Relay** transport.

### Single Web worker is an architecture requirement

The managed-media mutation fence is currently process-local. Deployed Web therefore runs one ASGI worker. Do not increase `WEB_CONCURRENCY`, start multiple Gunicorn/Uvicorn workers, or horizontally scale the Web process until the mutation fence has first been replaced with a database/distributed lock and corresponding concurrency regressions exist.

## Media and storage

### Upload by site type

Admin media upload does **not** select a concrete Follower. The site selector starts empty and the operator chooses one of three placement types:

- `primary` — Master Local / Local transport;
- `direct` — an eligible Direct Follower;
- `relay` — an eligible Relay Follower.

For Direct/Relay, FrontierCloud chooses among all ready members of that type that are storage-enabled, online, writable, and large enough for the object. Placement prefers lower `(used + reserved) / allocated` pressure and then more available bytes. Selection and durable upload reservation share the storage write lock so concurrent uploads see current reservations.

Historical media does not need migration for this model: existing owner/member/transport facts remain authoritative and the UI derives the visible site type from them.

### Capacity semantics

Admin deliberately separates physical observation from FrontierCloud quota:

- **physical used / all** — real filesystem usage and total capacity;
- **used / allocated** — FrontierCloud project usage and configured logical allocation;
- `reserved_bytes` — capacity held by in-progress uploads;
- `writable` / health — whether new placements are currently safe.

Logical allocation is never treated as physical free space.

### Folder priority, visibility, rename, and delete

Folders are first-class sorting objects. Public category/subcategory ordering is:

```text
directory priority descending -> name
```

Directory priority is separate from media-file priority and moves with a successful folder rename.

Admin supports controlled same-parent rename for real managed `music` / `vido` directories. Rename updates bytes and metadata together, joins the global media-mutation fence, coordinates all affected storage members on a Master, and rolls moved members back in reverse order if a later step fails. Active uploads, pending deletion state, a real target collision, or a required offline Follower block the operation. A deleted directory name may be reused when only stale directory metadata remains.

Visibility is inherited. Admin distinguishes a directly hidden object from an object hidden by an ancestor so it does not offer an ineffective "unhide" action at the wrong level.

Managed deletion uses recovery/journal semantics and converges pending remote deletes. Do not replace these workflows with direct `mv` / `rm` operations on `./data`.

## Lyrics and lyric relations

Lyrics are Master-owned business content even when the associated audio bytes live on Followers.

Supported hierarchy:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum. Upload, validation, Admin tree/search, download/delete collection, catalog, and relation management enforce the same boundary.

Admin supports both single-file and folder LRC upload. Folder upload preserves the supported relative directory structure. The relation browser shows recursive file counts for lyric and music directories.

One-click same-name auto-link matches exact music/LRC stems and prefers the same relative album path. Ambiguous duplicate lyric names are reported instead of guessed. Auto-link may fill a missing relation or replace the system fallback, but it never overwrites an explicit user-managed lyric relation.

`lyrics/default.lrc` is an internal playback fallback. It is generated/repaired at runtime, excluded from Admin trees/search/counts and user relation counts, and cannot be treated as ordinary user-managed lyric content.

Accepted lyric upload is complete only after both the file and its managed-object registration are durable. Registration failure rolls the newly published file back; a later cache-invalidation failure does not erase a committed lyric.

## Playback and karaoke

- LRC uploads support timestamps/offsets with a 2 MiB limit.
- Audio playback provides synchronized lyrics and fullscreen lyrics.
- Player sidebars show the current media directory relative path rather than a legacy fixed label.
- Playback scores and lyric links bind to stable media object IDs.
- Next-track preloading uses the player queue; offline switching requires a completed preload. Speculative downloads are capped at 128 MiB per track.
- Audio/video can enter Karaoke from the player button or a three-finger 1.5-second press. Guests can sing and preview in memory. Registered users receive recording quota, and the Master places recordings in the Storage Pool.

## Cluster, heartbeat, and Backup

FrontierCloud targets one-core VPS deployments. The product runtime accepts bounded streaming and ordinary request handling, but rejects burst or sustained high-load computation. Runtime transcoding, compression, inference, bulk transformation, subprocess execution, executor offload, and compute-heavy dependencies are outside the business boundary. CI enforces this statically, caps Web at one CPU, fails compute-caused timeouts, and requires CPU to return below 20% for five consecutive samples within 30 seconds after business tests.

Storage and Backup are independently configured on Followers. A saved desired setting is not considered effective until the Follower reports the observed state through authenticated heartbeat/control traffic.

Heartbeat health is intentionally independent from Backup work. Backup is asynchronous and a failed backup attempt does not turn a successful heartbeat into an offline node.

Backup sends bounded, chunked Master business-recovery artifacts, including LRC content, to enabled Backup Followers. Recovery points include integrity/checksum metadata. Backup is **not** live SQL replication, HA storage, automatic Master election, or automatic failover.

Nodes that still own protected media/recordings cannot be revoked or reinitialized, and configured storage capacity cannot be reduced below current use.

## Security and network observation

- IP views aggregate each address while MySQL retains the full event timeline.
- The first automatic ban lasts 24 hours; the second is permanent. Admin can release, permanently ban, or allowlist an address.
- Nginx enforces known bans before proxying, with a short propagation delay.
- WebRTC observation is mandatory. STUN uses `SERVER_NAME` and `WEBRTC_STUN_PORT`; probing starts on connection and repeats every 30 seconds.
- Admin provides public-IP-first and WebRTC-IP-first aggregate views.

## Admin console

The Admin console has an intentional, regression-tested module order:

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

Dynamic modules must join the same DOM and visual order; reordering logic is idempotent and browser-tested.

## Release management

FrontierCloud has exactly two canonical branches:

```text
dev   implementation + complete CI authority
main  reviewed release history
```

No feature/fix/release/temporary branches are allowed. Engineering changes go directly to the existing `dev`; the only valid PR into `main` is same-repository `dev -> main`. After a release PR merges, `dev` must be fast-forwarded to the resulting `main` merge commit before further work. Never force-rewrite either canonical branch.

A new `main` release is publishable only when FrontierCloud can prove:

1. current `main` HEAD is associated with the accepted merged `dev -> main` PR;
2. the exact promoted `dev` SHA has successful push CI;
3. the reviewed `dev` tree equals the `main` tree.

GitHub verification is cached and supports optional `GITHUB_API_TOKEN` authentication plus rate-limit backoff. If current verification is unavailable, the running service remains available but new upgrade authorization fails closed. Last-known-good release evidence is display-only and never authorizes a new upgrade.

System Release Management presents release state semantically: a converged cluster states that the current SHA matches `main` HEAD, while a real pending upgrade displays separate current and pending-release SHAs. Rollback uses the previous Web-managed SHA recorded by the Updater.

## Data and operations

Persistent state includes:

```text
./data           media, lyrics, recordings
mysql_data       durable business/catalog/cluster facts
redis_data       runtime cache/coordination
runtime_secrets  Admin/MySQL/metrics secrets
```

Back up MySQL, `./data`, and `runtime_secrets` as one recovery asset set. Restoring only one side can create catalog/filesystem drift.

Normal `docker compose down` preserves volumes. `docker compose down --volumes` destroys database and secret volumes and is not a normal upgrade or troubleshooting step.

Health endpoints:

```text
/health/live
/health/ready
/health
```

`/metrics` exposes Prometheus-compatible metrics using the generated Bearer token. Logs go to stdout/stderr. FrontierCloud does not deploy a monitoring/logging platform, dashboard, collector, or alerting system on its own.

## Checks

Local baseline:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
docker compose config --quiet
```

GitHub Actions on the exact final `dev` SHA is the release authority. The main workflow includes exact-promotion verification, real Chromium UI regression, real multi-node HTTPS cluster acceptance, Compose/source/configuration checks, unit/runtime tests, edge/security checks, and public/Admin flows.

CI cluster acceptance is fixed at three nodes: one Master, one Direct Follower, and one Relay Follower. The separate persistent private-CA validation installation uses one Master, three Direct Followers, and six Relay Followers. Its CD reconciler uses the same Admin “upgrade and distribute” transaction as an operator, verifies every application and Updater runtime SHA, and keeps maintenance closed on incomplete releases. Measured results are evidence for that host and dataset, not a throughput SLA.
