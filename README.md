# FrontierCloud

Self-hosted media browsing, playback, and administration. Docker Compose bundles Web/API, Nginx, MySQL, Redis, and the required WebRTC STUN service.

## Start

HTTP needs no configuration or `.env` file:

```bash
docker compose up -d --build --wait
```

Open `http://localhost`. Put audio in `data/media/music`, video in `data/media/vido`, and UTF-8 LRC lyrics in `data/media/lyrics`, or upload through Admin WebUI.

Initialization generates an Admin Key, two MySQL passwords, and a metrics Bearer token in the persistent `runtime_secrets` volume. The first successful Web startup prints them once:

```bash
docker compose logs web | grep initial_runtime_secrets
```

Container recreation can remove that log entry. Read the current key or token from the volume instead:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
# Database credentials use mysql_password and mysql_root_password in the same directory.
```

Keep secrets private. Restarts do not rotate them. Enter the Admin Key using the home page's privilege-elevation control; Admin WebUI can replace it with a random or confirmed custom key. Replacement invalidates other admin sessions. Sessions default to 180 minutes of inactivity; the key itself does not expire.

## HTTPS and configuration

Create `.env` with:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

Provide a matching certificate at `certs/fullchain.pem` and private key at `certs/privkey.pem`. HTTPS requires `SERVER_NAME`; HTTP does not. The project selects transport behavior from `TLS_ENABLED`, not deployment names.

[.env.example](.env.example) lists every supported setting and its purpose. Optional settings are commented and have technical defaults. Secrets are generated internally, not supplied through `.env`; remove unsupported legacy entries before upgrading. Alternative certificate paths and published ports are optional settings.

## Features and limits

- Admin manages uploads, downloads, visibility, deletion, and track-to-lyric links. One lyric can serve multiple tracks; each track has at most one lyric.
- LRC uploads support timestamps and offsets, with a 2 MiB limit. Audio playback shows four synchronized, smoothly scrolling lyric lines above the heartbeat; fullscreen lyrics remain available.
- Search is Admin-only, supports Simplified/Traditional Chinese and pinyin, and includes file paths. It searches the selected directory and descendants, up to one media-type root; global `data/media` queries are rejected. Results are capped at 200.
- Playback scores and lyric links bind to stable media object IDs. Next-track preloading uses the player's queue; offline switching requires a completed preload. Speculative downloads are capped at 128 MiB per track.
- IP views aggregate each address once and sort numerically. The first automatic ban lasts 24 hours; the second is permanent. Admin can release, permanently ban, or allowlist an IP. Nginx applies known bans before proxying, with a short propagation delay.
- WebRTC observation is mandatory. STUN uses `SERVER_NAME` and `WEBRTC_STUN_PORT`; probing starts on connection and repeats every 30 seconds. Admin displays aggregated IP relationships; MySQL retains event history.

## Data and operations

Media lives under host `./data`; MySQL, Redis, and secrets use `mysql_data`, `redis_data`, and `runtime_secrets` volumes. Ordinary `docker compose down` preserves them; `down --volumes` destroys database and secret volumes.

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
