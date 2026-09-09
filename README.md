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
- Lyric upload and track-to-lyric relation editing.
- Expandable modules with one module open at a time, without changing URL.
- IP state summaries, numeric IP ordering, manual release/permanent bans, and permanent allowlist management; the existing layout is retained.

The automatic security lifecycle is fixed: the first threshold violation blocks an IP for 24 hours, and the second violation permanently blacklists it. This timing is not configurable. An administrator may still explicitly release or allowlist an address in Admin WebUI.

### Lyrics

Static lyrics live in `data/media/lyrics`, alongside `data/media/music` and `data/media/vido`. Upload them from the existing media-management upload menu. Lyrics are reference-only objects: they never appear as standalone items in the public media catalog and do not have a visibility toggle. Supported files are UTF-8 `.txt` and `.json`, with a fixed 2 MiB limit. JSON may be a string array or an object whose `lines` field is a string array.

In the Lyrics module, the counters show current track, lyric, and relation totals. Select one track or lyric first and then enter linking mode. The graph displays only that selected object's edges, so a shared lyric does not turn the entire catalog into a spider web. A track has zero or one lyric; a lyric may be reused by any number of tracks. Saving a track relation replaces its earlier lyric. Saving from a lyric replaces the complete set of tracks that reference that lyric. Deleting a track, lyric, or containing directory removes its relationships in the same MySQL transaction as the existing media metadata cleanup.

For an associated song, the audio player uses the full area above the heartbeat line for a fixed two-column lyric view and enables its Fullscreen Lyrics control. The old album-cover disc is not rendered. The control opens a dedicated blank lyric window without exposing a standalone lyric index; that fullscreen view always uses three columns. Both views size text from the actual line count and viewport and use a fixed, muted color cycle with bold static text. A song without a relation hides the inline lyric and keeps the fullscreen control disabled.

Each IP is one object across the security page, including the allowlist. Filtering and pagination operate on current objects, not historical events. The IP sort control replaces the old time range: IPv4 is compared by its four numeric octets (`10.199.254.235 < 13.11.1.1`); IPv6 is compared by its 128-bit value, after IPv4 in ascending order. Allowlisted objects appear only in the allowlist section of the same paginated result. The statistics count all current objects, independently of the page/filter.

Detailed investigations belong in MySQL. `ip_auto_ban_events` retains all historical bans and their effective expiry/release timestamps. New classified invalid requests and security actions are appended to `ip_security_audit_log`; a state change and its audit entry commit or roll back together. Whitelist removal no longer removes its audit evidence. Earlier deployments' missing per-request/whitelist details cannot be reconstructed retroactively. Connect with the generated database credentials and query, for example:

```sql
SELECT created_at, id, action, detail, session_id_hash
FROM ip_security_audit_log WHERE ip_address = '203.0.113.9'
ORDER BY created_at, id;
SELECT * FROM ip_auto_ban_events WHERE ip_address = '203.0.113.9'
ORDER BY banned_at, id;
```

The removed `SECURITY_RECENT_BAN_HOURS` variable is no longer supported; remove it from any existing deployment `.env` before upgrading. There is no replacement time-range variable or web history view.

### Edge enforcement

The legal API statistic counts documented method/path operations under `/api/` using FastAPI's public OpenAPI schema; hidden HTML views, HEAD/OPTIONS, health checks and static files are excluded. This also supports nested/lazily included routers without relying on private routing internals.

Known bans are enforced by native Nginx `geo` rules against the socket peer address, before proxying or serving static content. Client-supplied `X-Real-IP`/forwarding headers cannot bypass this check. MySQL remains authoritative: after a committed security change, Web atomically publishes `data/.ip-security/active-bans.tsv`, outside the media tree. Nginx reads this existing read-only data mount; no extra container, port, Docker socket, package, or environment variable is needed. A small shell helper checks the local snapshot once per second and validates/reloads Nginx only when the effective IP set changes, including expiration. See the [Nginx geo documentation](https://nginx.org/en/docs/http/ngx_http_geo_module.html).

Allowlisting, manual release and expiration remove the edge rule. Updates normally propagate in about 1–2 seconds, not synchronously with the admin response. A malformed/missing snapshot or rejected Nginx configuration retains the last valid rules and logs an error. Failed Web publication is retried every five seconds; initial publication failure prevents Web readiness. The backend check remains as defense during propagation and for direct internal access. First-seen invalid requests still reach the application for route classification; existing in-flight requests are not retroactively cancelled by a graceful reload.

Classified violations and state changes are durable MySQL audit records. Requests already rejected at the edge stay in Nginx stdout logs (`security_blocked=1`, `upstream_addr="-"`), not per-request MySQL writes: otherwise a blocked scanner would still cause backend/database load. Expiration is determined by each ban's stored `expires_at`; it does not require a page visit or a synthetic audit row. Investigation combines the database lifecycle with edge access logs when individual rejected requests are needed.

## Audio playback caching

Audio playback preloads the next item from the same page-local queue used by the Next control. Preloading starts after five seconds (earlier for short tracks), retries transient failures, and uses a completed Blob directly when switching. Score changes update displayed values; ordering is recalculated when opening a player page, not mid-queue. At most the current cached track and one upcoming track are retained, with a 128 MiB limit per speculative download. Oversized audio and videos use normal streaming. Offline switching requires the next download to have completed; early manual skips or interrupted downloads still require network access. The player does not automatically mute after inactivity.

Public media requests are path-validated and visibility-authorized by Web, then transferred by Nginx through an internal-only media location. Nginx `sendfile` and byte-range handling keep large audio/video bodies out of the Python worker while preserving seek and preload behavior. General public and API requests accept at most 64 KiB bodies; only authenticated Admin upload routes permit large request bodies, up to the application upload limit.

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
- Lyrics and their files: host `./data/media/lyrics`; track relationships: MySQL `media_lyric_links`.
- MySQL: Docker `mysql_data` volume.
- Redis: Docker `redis_data` volume.
- Generated secrets: Docker `runtime_secrets` volume.
- Derived edge ban snapshot: `data/.ip-security/`, rebuilt from MySQL at Web startup; no secrets or media are stored there. Do not delete it as a live unban mechanism; use Admin WebUI.

An ordinary `docker compose down` preserves these volumes. Only an explicit destructive operation with `--volumes` removes the databases and runtime secrets. The next startup then performs a fresh initialization and generates new values.

Media deletion first moves the selected objects into `data/media/.delete-<operation-id>` on the same filesystem. A MySQL journal records whether metadata deletion committed. Web startup restores pending operations and removes committed quarantine data. Do not manually remove a quarantine directory or its `media_delete_operations` row while recovery is pending. If a commit outcome cannot be determined, further media mutations return HTTP 503; restore database availability and restart Web (`docker compose restart web`) to run recovery. Incomplete rollback recovery prevents startup so new uploads cannot overwrite files awaiting restoration. These filesystem mutation locks cover the single Web worker shipped by the project; shared-media multi-worker or multi-replica deployment is not supported by this recovery mechanism.

Schema migrations use individually atomic MySQL DDL statements under a named connection lock; the entire startup migration is not one rollbackable transaction. IP security changes commit to MySQL before rebuilding Redis. A dirty or unavailable security cache is read from MySQL until it can be rebuilt. No extra configuration variables are required for these transaction safeguards.

## Development checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node --check static/js/admin.js
node --check static/js/network-observation.js
node --check static/js/lyrics.js
node tests/admin_ui_smoke.mjs
node tests/player_cache_smoke.mjs
docker compose config --quiet
docker compose up -d --build --wait
curl -fsS http://localhost/health/ready
```

After CI passes on `dev`, the RN preproduction environment fast-forwards to that tested commit and runs HTTPS verification. `main` receives changes only through a reviewed pull request.
