# Deployment and Configuration

## 1. Supported deployment shape

Recommended host requirements:

- Linux;
- Docker Engine;
- Docker Compose v2;
- sufficient disk for `./data`, Redis, images, recovery data and optional MySQL;
- valid DNS/certificate conditions before fixing a node into Master/Follower.

FrontierCloud assumes one Web process: native Go by default, or one ASGI worker
in the explicit Python reference profile. Do not horizontally replicate Web or
increase Python worker counts. Native OS leases protect shared-volume mutation;
they do not create a second business authority.

## 2. Minimal Standalone startup

For a fresh native HTTP Standalone node, select an exact committed image revision:

```bash
export FRONTIERCLOUD_REVISION="$(git rev-parse HEAD)"
bash scripts/build-native-images.sh "$FRONTIERCLOUD_REVISION"
docker compose up -d --no-build --wait
```

Check status:

```bash
docker compose ps
```

Health:

```bash
curl -fsS http://127.0.0.1/health/live && echo
curl -fsS http://127.0.0.1/health/ready && echo
```

Follow logs:

```bash
docker compose logs -f --tail=200
```

The default is Go + SQLite. To select Go + MySQL, Python + SQLite or Python +
MySQL through `.env`, use the four configurations in
[Native deployment](../../protocol/v2/native-deployment.md). Use a distinct
project/data root for a new instance. Changing the selection never migrates an
existing database or grants historical cluster startup admission.

## 3. HTTPS and fixed roles

Create `.env`:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
# Also persist the exact native image SHA if not exported by the operator:
# FRONTIERCLOUD_REVISION=<40-character committed SHA>
```

Default certificate files:

```text
certs/fullchain.pem
certs/privkey.pem
```

Optional path overrides are documented in `.env.example`.

The certificate must match `SERVER_NAME`.

Standalone may run HTTP. A fixed Master or Follower requires certificate-verified HTTPS and fails startup rather than silently downgrading if the TLS requirement is later broken.

## 4. Main Compose services

```text
secrets-init
media-init
updater
web
redis
nginx
stun
```

MySQL is an additional service only in the MySQL overlay; SQLite uses the private
`data/frontiercloud.db` file and requires no MySQL server.

### secrets-init

Initializes persistent runtime secrets:

- MySQL application password;
- MySQL root password;
- Admin Key;
- metrics Bearer token.

Secrets live in the persistent `runtime_secrets` volume and are not ordinary `.env` settings.

Startup logs list newly created secret names without printing values. Restarts reuse the persistent values instead of rotating them.

### media-init

Initializes the managed host data tree under `./data` and grants the unprivileged Web process the required permissions.

### web

Runs native Gin business/control logic, Admin, catalog, playback metadata,
lyrics, cluster control and observability. The explicit Python reference profile
runs FastAPI instead. Native initialization, migration, health checks,
maintenance and the default updater use Go binaries, not Python wrappers.

Production Web must remain single-worker while the media mutation fence is process-local.

### updater

Owns Web-managed release build/replace, upgrade, rollback, maintenance state, and cluster distribution.

### mysql / redis

The selected SQLite or MySQL backend stores durable business and cluster facts.
Redis is runtime cache/coordination and is not the sole durable business store.

### nginx

Public HTTP/HTTPS entry point for TLS, proxying, static resources, maintenance behavior, IP edge enforcement, and internal endpoint request-size boundaries.

### stun

Provides the STUN service required by WebRTC observation.

## 5. Persistent state

Important persistent locations:

```text
./data                 SQLite DB, media, lyrics, recordings, recovery state
mysql_data             optional MySQL durable state
redis_data             Redis runtime state
runtime_secrets        Admin/MySQL/metrics secrets
updater_control        Web-to-Updater control channel
maintenance_state      release maintenance state
```

Normal shutdown:

```bash
docker compose down
```

preserves named volumes.

This is destructive:

```bash
docker compose down --volumes
```

It removes database/secret volumes and is not a normal upgrade or troubleshooting step.

## 6. Common environment settings

`.env.example` is the supported configuration contract. Common entries include:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
INSTANCE_NAME=frontiercloud
LOG_LEVEL=INFO
LOG_FORMAT=json
MYSQL_DATABASE=office_automation
MYSQL_USER=media_admin
WEBRTC_STUN_PORT=3478
PUBLIC_BIND_ADDRESS=0.0.0.0
```

Use:

```dotenv
PUBLIC_BIND_ADDRESS=::
```

only when the host/network are intentionally prepared to publish IPv6.

## 7. Optional GitHub release-verification token

On the Master, a least-privilege read-only token can be configured as:

```dotenv
GITHUB_API_TOKEN=...
```

It is used only server-side for GitHub REST release verification. FrontierCloud also caches verification results and backs off on 403/429 rate limiting. Current verification failure never authorizes a new upgrade from stale evidence.

## 8. Reading generated secrets

Admin Key:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
```

Metrics token:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

MySQL application password:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/mysql_password'
```

Do not paste these values into public logs, issues, PRs, or screenshots.

## 9. First Admin access

Rapidly click the second half of the home logo five times to enter the Admin Key. Rapidly click the first half five times to force a UI cache refresh.

Admin supports:

- replacing the long-term key with a random or confirmed custom key;
- single-use temporary keys;
- 15 / 30 / 60 / 120 minute sliding temporary sessions;
- persistent sessions with a default 180 minute inactivity window.

The inactivity window follows trusted pointer, keyboard, touch, or wheel activity in the Admin page. Automatic storage, node, security, and release-status polling is passive: it may validate the current session, but it does not renew the Redis TTL or browser cookies. Leaving an Admin tab open therefore cannot keep a session alive indefinitely.

Replacing the long-term key invalidates other Admin sessions and unused temporary keys.

## 10. Role initialization

Recommended order for the first cluster deployment:

```text
1. Start every node successfully as Standalone
2. Enable HTTPS and verify certificates/DNS
3. Fix the intended business node as Master
4. Fix resource nodes as Followers
5. Pair Followers to the Master
6. Verify active relationships and recent heartbeat
7. Configure Storage and/or Backup
8. Wait for Desired == Observed
9. Test upload, playback/download, Backup, and release status
```

Do not operate a Follower as an independent second business Master.

## 11. Storage configuration

Storage capacity has two distinct dimensions:

```text
physical filesystem capacity
FrontierCloud logical allocation
```

Admin presents physical used/total separately from FrontierCloud used/allocated. A member may have logical allocation remaining but still be unwritable because real filesystem space, reservations, health, or relationship state make placement unsafe.

The Master Local member is `primary`. Follower Storage transport may be Direct or Relay.

Changing Direct/Relay mode is blocked while durable upload reservations exist, because transport meaning must not change mid-upload.

## 12. Media upload placement

Admin media upload starts with **no site type selected**. The operator must explicitly choose:

- `primary` — Master Local;
- `direct` — eligible Direct Followers;
- `relay` — eligible Relay Followers.

The operator does not choose a specific Follower for Direct/Relay. FrontierCloud chooses among ready members of the selected type using current allocation pressure, reservations, physical availability, health, and writable state.

## 13. Lyrics paths

Managed lyric layout is bounded:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum.

`lyrics/default.lrc` is runtime-generated internal fallback content. Do not add it back to Git tracking under `data/`; the repository source contract intentionally keeps runtime `data/` outside releases.

## 14. Backup configuration

Backup is configured independently from Storage. An enabled Backup setting becomes effective only after the Follower reports the observed state.

Backup transfers use authenticated internal endpoints and bounded chunks. The dedicated Nginx Backup route accepts the application control-message size needed for encoded chunks instead of inheriting the smaller generic control limit.

Backup is a business recovery artifact, not live SQL replication or automatic failover.

## 15. Health and metrics

Liveness:

```text
/health/live
```

Readiness:

```text
/health/ready
/health
```

Prometheus-compatible metrics:

```text
/metrics
```

The metrics endpoint requires the generated Bearer token.

## 16. Logging

Services log to stdout/stderr. Typical commands:

```bash
docker compose logs -f web
docker compose logs -f updater
docker compose logs -f nginx
docker compose logs -f mysql
docker compose logs -f redis
```

FrontierCloud does not deploy a monitoring/logging platform, dashboard, collector, or alert rules on its own.

## 17. Production release flow

Do not update production by manually checking out arbitrary commits on every node.

Normal path:

```text
dev change
 -> exact dev push CI
 -> reviewed same-repo dev -> main PR
 -> merge
 -> release verifier proves provenance/tree/CI
 -> Admin starts upgrade
 -> Master build/replace
 -> Follower distribution
 -> cluster convergence
```

After a release PR merges, fast-forward `dev` to the resulting `main` merge commit before continuing development.

## 18. Backup the system as one recovery set

At minimum, preserve together:

```text
MySQL
./data
runtime_secrets
```

Restoring only the database or only the filesystem can create catalog/filesystem drift. Treat these as one recovery asset set.

## 19. Unsupported shortcuts

Do not use these as routine administration:

- `docker compose down --volumes` for upgrades;
- direct `rm`/`mv` on managed media to bypass catalog operations;
- manual Schema Generation edits;
- blind SQL table drops to fix migration errors;
- multiple Web workers as a performance tweak;
- direct business writes on Followers;
- resurrecting Compute Worker configuration from historical tables.
