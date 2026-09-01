# FrontierCloud

FrontierCloud is a FastAPI media service deployed with Docker Compose. Its
business stack contains Nginx, MySQL, Redis, and a STUN-only coturn service.

## Configuration contract

`.env.example` is the authoritative list of supported FrontierCloud settings.
Required settings are active entries; optional settings are commented and use
the stable technical defaults shown. Operator-private hostnames, credentials,
one-time values, and surrounding infrastructure settings do not belong there.

```bash
cp .env.example .env
chmod 600 .env
```

Replace the required database placeholders with independent random secrets.
`MYSQL_URL` is normally derived from `MYSQL_USER`, `MYSQL_PASSWORD`, and
`MYSQL_DATABASE`; use its optional override only for a different endpoint.

Transport is selected directly:

- `TLS_ENABLED=false` runs the private development deployment over HTTP.
- The default `TLS_ENABLED=true` runs the RN and DMIT deployments over HTTPS.

Configuration changes must update the reader, `.env.example`, this document,
and contract tests together.

## Deployment

Requirements are Git, Docker Engine, and Docker Compose v2. Keep SELinux
enabled on production hosts.

```bash
sudo install -d -m 0755 data data/media data/media/music data/media/vido certs certs/acme
sudo chown -R 10001:10001 data
sudo chmod 0700 certs
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d --build --remove-orphans --wait --wait-timeout 240
sudo docker compose exec -T nginx nginx -t
```

Never run `docker compose down --volumes` on a persistent deployment. The `dev`
branch supplies private development and RN preproduction; `main` supplies DMIT
production. RN CD runs only after the complete `dev` CI job passes.

## Observability boundary

FrontierCloud exposes consumer-neutral interfaces and does not deploy or manage
an external monitoring, dashboard, alerting, or log-processing platform.

- `GET /health/live` reports process liveness.
- `GET /health/ready` and `GET /health` verify MySQL and Redis readiness.
- `GET /metrics` emits Prometheus-compatible application metrics when the
  optional `METRICS_TOKEN` is configured. Send it as
  `Authorization: Bearer TOKEN`; an unset token disables the endpoint.
- Application and Nginx logs go to stdout/stderr. JSON is the default and
  includes timestamp, level, component, request ID, trace ID, instance identity,
  status, latency, and safe request context. `LOG_FORMAT=text` is available for
  local use.

Any standards-compatible external consumer may collect these interfaces.
FrontierCloud does not know or control which consumer is used.

## Administrator access

Administrator tokens have a short, dynamic lifecycle. No static bootstrap token
is configured. The service periodically issues a token and writes the current
token to the structured web-container log:

```bash
sudo docker compose logs -f web | grep admin_token_issued
```

An operator may generate another dynamic token on demand inside the running web
container:

```bash
sudo docker compose exec -T web python -m app.services.admin_token_cli
```

## Standalone download tools

Operator-owned download helpers live under `scripts/auto_download/`. They are
not application packages, are excluded from the business image, and do not run
in the service lifecycle. Install the optional workstation dependencies and run
a tool as a module, for example:

```bash
python -m pip install -e ".[tools]"
python -m scripts.auto_download.yt_mp3 --help
```

Audio belongs under `data/media/music` and video under `data/media/vido`.

## Verification

```bash
python scripts/check_english_comments.py
python -m unittest discover -s tests -p "test_*.py" -v
docker compose config --quiet
```
