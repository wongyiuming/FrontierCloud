# FrontierCloud

Self-hosted media browsing, playback, karaoke, and administration. FastAPI provides the business API and native browser JavaScript provides the media UI and real-time karaoke session. Docker Compose bundles Web/API, Nginx, MySQL, Redis, and the required WebRTC STUN service.

## Start

HTTP needs no configuration or `.env` file:

```bash
docker compose up -d --build --wait
```

Open `http://localhost`. Put audio in `data/media/music`, video in `data/media/vido`, and UTF-8 LRC lyrics in `data/media/lyrics`, or upload through Admin WebUI.
The startup initializer creates these directories and gives the unprivileged Web process access to the mounted `data` tree.

Initialization generates an Admin Key, two MySQL passwords, and a metrics Bearer token in the persistent `runtime_secrets` volume. Startup logs list newly created secret names without printing values. Read the current key or token with:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
# Database credentials use mysql_password and mysql_root_password in the same directory.
```

Keep secrets private. Restarts do not rotate them. Rapidly click the second half of the home logo five times to enter the Admin Key; rapidly click the first half five times to force a UI cache refresh. Admin WebUI can replace the key with a random or confirmed custom key, or issue a single-use temporary key with a 15, 30, 60, or 120 minute sliding session. Replacement invalidates other admin sessions and unused temporary keys. Persistent sessions default to 180 minutes of inactivity; the long-term key itself does not expire.

## HTTPS and configuration

Create `.env` with:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

Provide a matching certificate at `certs/fullchain.pem` and private key at `certs/privkey.pem`. HTTPS requires `SERVER_NAME`; HTTP does not. The project selects transport behavior from `TLS_ENABLED`, not deployment names.
Public ports bind IPv4 by default. Set `PUBLIC_BIND_ADDRESS=::` in `.env` only when the host is ready to publish IPv6.

[.env.example](.env.example) lists every supported setting and its purpose. Optional settings are commented and have technical defaults. Secrets are generated internally, not supplied through `.env`; remove unsupported legacy entries before upgrading. Alternative certificate paths and published ports are optional settings.

## Features and limits

- Admin manages uploads, downloads, visibility, deletion, and track-to-lyric links. One lyric can serve multiple tracks; each track has at most one lyric.
- LRC uploads support timestamps and offsets, with a 2 MiB limit. Audio playback shows four synchronized, smoothly scrolling lyric lines above the heartbeat; fullscreen lyrics remain available.
- The current audio or video can enter karaoke from its player button or a three-finger 1.5-second press. The page reuses the selected media and Master-owned lyrics, supports separate input and output devices where available, and records only the microphone voice bus. Guests can sing and preview in memory. New accounts receive 200 MiB; the Master automatically places recordings in the storage pool, and downloaded recordings carry synchronized lyric metadata for later re-upload.
- Search is Admin-only, supports Simplified/Traditional Chinese and pinyin, and includes file paths. It searches the selected directory and descendants, up to one media-type root; global `data/media` queries are rejected. Results are capped at 200.
- Playback scores and lyric links bind to stable media object IDs. Next-track preloading uses the player's queue; offline switching requires a completed preload. Speculative downloads are capped at 128 MiB per track.
- Nodes default to Standalone. HTTP remains Standalone; fixed Master/Follower roles require certificate-verified HTTPS and fail closed if TLS is later unavailable. A cluster has one business Master and any number of resource Followers. Followers expose node management, health, metrics, signed data transfer, backup, and worker APIs; public pages go to the Master and business APIs are rejected centrally.
- The Master owns one global media catalog and a storage pool containing its fixed-capacity Local member plus enabled Followers. One logical path identifies one complete media object at exactly one member. Admin selects a writable member before media upload; playback and download resolve Local, Direct, or Relay transport transparently. Lyrics, associations, playback facts, users, and audit facts remain on the Master.
- Storage, Compute, and Backup are configured independently for each Follower. Followers can execute leased, retryable work near their files and asynchronously store bounded-memory, chunked Master business backups, including LRC content. Backups are recovery artifacts, not online replicas or automatic failover. Nodes containing media or recordings cannot be revoked or reinitialized, and capacity cannot be reduced below use.
- IP views aggregate each address once and sort numerically or by its last attack. The summary separates observed, active, historical, permanent, and allowlisted addresses while MySQL retains the full event timeline. The first automatic ban lasts 24 hours; the second is permanent. Admin can release, permanently ban, or allowlist an IP. Nginx applies known bans before proxying, with a short propagation delay.
- WebRTC observation is mandatory. STUN uses `SERVER_NAME` and `WEBRTC_STUN_PORT`; probing starts on connection and repeats every 30 seconds. Admin shows public-IP-first and WebRTC-IP-first aggregate views; MySQL is the source of truth for complete event history and aggregate state.

## Data and operations

Media, lyrics, and recordings live under host `./data`; MySQL, Redis, and secrets use `mysql_data`, `redis_data`, and `runtime_secrets` volumes. Back up the database, data, and secrets together. Restarts retain roles; only explicit Admin reinitialization resets an empty node. Fixed Master/Follower roles require TLS on restart; disabling TLS fails startup instead of resetting or downgrading the role. Ordinary `docker compose down` preserves data; `down --volumes` destroys database and secret volumes.

Do not rename or move managed files directly: paths locate objects, but cannot declare identity changes. No move API is currently provided. Deletion uses a MySQL journal and temporary quarantine; do not manually remove pending recovery data. If recovery blocks mutations, restore MySQL and restart Web. Shared-media multi-worker/multi-replica operation is not supported by this recovery mechanism.

`/health/live` reports liveness; `/health/ready` and `/health` report readiness. `/metrics` exposes Prometheus-compatible metrics with the generated Bearer token. Logs go to stdout/stderr in JSON or text. FrontierCloud does not deploy or manage monitoring/log platforms, dashboards, collectors, or alert rules.

## Checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
docker compose config --quiet
```

Changes enter `main` through reviewed pull requests. Release promotion validates the merged PR provenance, exact successful `dev` push CI result, and identical reviewed `dev` / `main` trees, so release correctness does not depend on which GitHub merge method produced `main`.
