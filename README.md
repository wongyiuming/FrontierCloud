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

## HTTPS

HTTPS requires enabling TLS, setting the public hostname, and providing a certificate:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

The default certificate paths are `./certs/fullchain.pem` and `./certs/privkey.pem`. Enable the corresponding optional entries in `.env.example` to change them. Startup fails when TLS is enabled without a valid `SERVER_NAME`. HTTP mode has no required variables.

The complete formal configuration contract is [`.env.example`](.env.example). Every optional variable remains commented, and omission selects the documented technical default. Database passwords and the Admin Key are initialization-generated runtime secrets, not environment variables. `.env` may contain only variables listed by that contract; CI and RN deployment reject unknown names before Compose starts.

## Admin WebUI

Open the admin login from the media home page and enter the current Admin Key. The admin view provides:

- Random or custom Admin Key rotation.
- Media upload, download, visibility, and deletion controls.
- An always-visible IP security view with audit history, manual release, and permanent allowlist management.

The automatic security lifecycle is fixed: the first threshold violation blocks an IP for 24 hours, and the second violation permanently blacklists it. This timing is not configurable. An administrator may still explicitly release or allowlist an address in Admin WebUI.

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

## Development checks

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node --check static/js/admin.js
node --check static/js/network-observation.js
docker compose config --quiet
docker compose up -d --build --wait
curl -fsS http://localhost/health/ready
```

After CI passes on `dev`, the RN preproduction environment fast-forwards to that tested commit and runs HTTPS verification. `main` receives changes only through a reviewed pull request.
