# FrontierCloud

FrontierCloud is a self-hosted media browsing, playback, and administration service. The project bundles the Web/API application, Nginx, MySQL, Redis, and the mandatory WebRTC STUN service.

## Quick start

An HTTP development deployment requires no environment values and no `.env` file:

```bash
docker compose up -d --build --wait
docker compose logs web
```

The first initialization generates three independent strong random values: the Admin Key, the MySQL application password, and the MySQL root password. They live in the Docker `runtime_secrets` volume and are never written to the repository or `.env`. The first successful Web startup prints them once in the structured `initial_runtime_secrets` log entry:

```bash
docker compose logs web | grep initial_runtime_secrets
```

Save these values immediately. Restarts neither rotate nor print them again. The Admin Key has no automatic expiration or scheduled rotation. A holder of the current key can enter Admin WebUI and either generate a new random strong key or enter and confirm a custom key. Rotation immediately invalidates every other admin session.

`docker compose logs web` only reads the current Web container. If CD or a manual deployment has recreated that container, the one-time initialization entry may no longer be available. The generated values are still stored in the persistent `runtime_secrets` volume and can be read by a host administrator at any time:

```bash
# Current Admin Key
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'

# Current generated database passwords
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/mysql_password'
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/mysql_root_password'
```

These commands print secrets to the terminal, so run them only in a private administrator session and do not paste their output into tickets or logs.

## HTTPS

HTTPS requires enabling TLS, setting the public hostname, and providing a certificate:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

The default certificate paths are `./certs/fullchain.pem` and `./certs/privkey.pem`. Enable the corresponding optional entries in `.env.example` to change them. Startup fails when TLS is enabled without a valid `SERVER_NAME`. HTTP mode has no required variables.

The complete formal configuration contract is [`.env.example`](.env.example). Every optional variable remains commented, and omission selects the documented technical default. Database passwords and the Admin Key are initialization-generated runtime secrets, not environment variables. `.env` may contain only variables listed by that contract; CI and RN deployment reject unknown names before Compose starts.

## Admin WebUI

Open the media home page, select the privilege-elevation control (`id="elevate"`), and enter the current Admin Key. After login, use the Admin Key panel to generate a random replacement or enter the same custom replacement twice. Save the returned new key immediately; it becomes the only valid key and every other admin session is invalidated. The admin view also provides:

- Random or custom Admin Key rotation.
- Media upload, download, visibility, and deletion controls.
- An always-visible IP security view with audit history, manual release, and permanent allowlist management.

The automatic security lifecycle is fixed: the first threshold violation blocks an IP for 24 hours, and the second violation permanently blacklists it. This timing is not configurable. An administrator may still explicitly release or allowlist an address in Admin WebUI.

## Audio playback caching

Audio playback preloads the next item from the same page-local queue used by the Next control. Preloading starts after five seconds (earlier for short tracks), retries transient failures, and uses a completed Blob directly when switching. Score changes update displayed values; ordering is recalculated when opening a player page, not mid-queue. At most the current cached track and one upcoming track are retained, with a 128 MiB limit per speculative download. Oversized audio and videos use normal streaming. Offline switching requires the next download to have completed; early manual skips or interrupted downloads still require network access. The player does not automatically mute after inactivity.

## WebRTC network observation

WebRTC is a core FrontierCloud function, so Compose always starts the STUN service. The browser uses this derived address without a separate URL setting:

```text
stun:<SERVER_NAME>:<WEBRTC_STUN_PORT>
```

The first probe starts with the initial page connection and repeats every 30 seconds by default. `WEBRTC_STUN_PORT` changes the port, and `WEBRTC_REPORT_COOLDOWN` changes the probe period. There is no `WEBRTC_STUN_URLS` setting.

## Observability boundary

FrontierCloud only exposes standard interfaces that an external system can consume:

- `/health/live` for process liveness.
- `/health/ready` and `/health` for MySQL, Redis, and application readiness.
- `/metrics` for Prometheus-compatible metrics; it is available only after configuring `METRICS_TOKEN` and requires its Bearer token.
- stdout/stderr structured logs in JSON or text, with timestamp, level, component, request ID, trace ID, instance identity, and safe request context.

The project does not deploy or manage Prometheus, Grafana, Elasticsearch, Logstash, Kibana, ELK, dashboards, scrape targets, or alert rules. It has no consumer-specific metrics or log coupling. Health and metrics probes are excluded from Nginx access logs, and Docker bridge peers are not repeated as business client fields.

## Data lifecycle

- Media: host `./data` directory.
- MySQL: Docker `mysql_data` volume.
- Redis: Docker `redis_data` volume.
- Generated secrets: Docker `runtime_secrets` volume.

An ordinary `docker compose down` preserves these volumes. Only an explicit destructive operation with `--volumes` removes the databases and runtime secrets. The next startup then performs a fresh initialization and generates new values.

Media deletion first moves the selected objects into `data/media/.delete-<operation-id>` on the same filesystem. A MySQL journal records whether metadata deletion committed. Web startup restores pending operations and removes committed quarantine data. Do not manually remove a quarantine directory or its `media_delete_operations` row while recovery is pending. If a commit outcome cannot be determined, further media mutations return HTTP 503; restore database availability and restart Web (`docker compose restart web`) to run recovery. Incomplete rollback recovery prevents startup so new uploads cannot overwrite files awaiting restoration. These filesystem mutation locks cover the single Web worker shipped by the project; shared-media multi-worker or multi-replica deployment is not supported by this recovery mechanism.

Schema migrations use individually atomic MySQL DDL statements under a named connection lock; the entire startup migration is not one rollbackable transaction. IP security changes commit to MySQL before rebuilding Redis. A dirty or unavailable security cache is read from MySQL until it can be rebuilt. No extra configuration variables are required for these transaction safeguards.

## Development checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node --check static/js/admin.js
node --check static/js/network-observation.js
node tests/player_cache_smoke.mjs
docker compose config --quiet
docker compose up -d --build --wait
curl -fsS http://localhost/health/ready
```

After CI passes on `dev`, the RN preproduction environment fast-forwards to that tested commit and runs HTTPS verification. `main` receives changes only through a reviewed pull request.
