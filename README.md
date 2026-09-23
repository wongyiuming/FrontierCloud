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
- The current audio or video can enter karaoke from its player button or a three-finger 1.5-second press. The page reuses the selected media and owner-bound lyrics, supports separate input and output devices where available, and records only the microphone voice bus. Guests can sing and preview in memory. Karaoke accounts can upload voice recordings to one explicitly selected Slave storage node; new accounts receive 200 MiB, and downloaded recordings carry their synchronized lyric metadata for later re-upload.
- Search is Admin-only, supports Simplified/Traditional Chinese and pinyin, and includes file paths. It searches the selected directory and descendants, up to one media-type root; global `data/media` queries are rejected. Results are capped at 200.
- Playback scores and lyric links bind to stable media object IDs. Next-track preloading uses the player's queue; offline switching requires a completed preload. Speculative downloads are capped at 128 MiB per track.
- Nodes default to Standalone. With working, certificate-verified HTTPS, Admin can fix a node's role as Master or Slave and import a Slave's five-minute, one-time pairing package. Each relationship is independent; Slave retains its own public pages and Admin. Master merges directories, preserves distinct objects at identical paths, and selects Relay (Nginx) or Direct (short resource token) per relationship. Public pages do not expose topology.
- A Master can enable a downstream Slave for user recordings and assign its capacity. The Master itself cannot store user recordings. Direct relationships transfer recording bytes to the Slave; Relay relationships stream them through the Master. Disabling, revoking, or reinitializing a node that still holds user recordings is rejected.
- Remote media identity combines its owning node and original object ID. Media and existing lyrics/attachments resolve on the same owner; cross-node attachment links are not supported. Master playback records take precedence; absent Master records use the owner's catalog data. Master cannot mutate Slave files. Offline relationships retain catalogs and recover automatically; Admin can revoke one relationship or explicitly reinitialize a node without deleting media.
- IP views aggregate each address once and sort numerically or by its last attack. The summary separates observed, active, historical, permanent, and allowlisted addresses while MySQL retains the full event timeline. The first automatic ban lasts 24 hours; the second is permanent. Admin can release, permanently ban, or allowlist an IP. Nginx applies known bans before proxying, with a short propagation delay.
- WebRTC observation is mandatory. STUN uses `SERVER_NAME` and `WEBRTC_STUN_PORT`; probing starts on connection and repeats every 30 seconds. Admin shows public-IP-first and WebRTC-IP-first aggregate views; MySQL is the source of truth for complete event history and aggregate state.

## Data and operations

Media and Slave-owned recordings live under host `./data`; MySQL, Redis, and secrets use `mysql_data`, `redis_data`, and `runtime_secrets` volumes. Back up the database, data, and secrets together: node roles, identities, credentials, catalogs, users, quotas, recording metadata, and files must remain consistent. Restarts and rebuilds retain roles; only explicit Admin reinitialization resets one. Fixed Master/Slave roles require TLS on restart; disabling TLS fails startup instead of resetting or downgrading the role. Ordinary `docker compose down` preserves them; `down --volumes` destroys database and secret volumes.

Do not rename or move managed files directly: paths locate objects, but cannot declare identity changes. No move API is currently provided. Deletion uses a MySQL journal and temporary quarantine; do not manually remove pending recovery data. If recovery blocks mutations, restore MySQL and restart Web. Shared-media multi-worker/multi-replica operation is not supported by this recovery mechanism.

`/health/live` reports liveness; `/health/ready` and `/health` report readiness. `/metrics` exposes Prometheus-compatible metrics with the generated Bearer token. Logs go to stdout/stderr in JSON or text. FrontierCloud does not deploy or manage monitoring/log platforms, dashboards, collectors, or alert rules.

## Checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
docker compose config --quiet
```

Changes enter `main` through reviewed pull requests.
