# FrontierCloud

FrontierCloud is a self-hosted FastAPI service for media browsing and controlled
media administration, document watermarking, network observation, security
auditing, and production monitoring. The application stack is deployed with
Docker Compose and is designed for CentOS hosts with SELinux enabled.

## What is included

- FastAPI application running as an unprivileged, read-only container
- Nginx public gateway with environment-specific HTTP or TLS behavior
- MySQL 8.4 for persistent application and audit data
- Redis for admin sessions, catalog caching, rate limits, and IP security state
- A STUN-only coturn service for client network observation
- Collection agents in every environment:
  - Node Exporter
  - cAdvisor
  - Redis Exporter
  - MySQL Exporter
  - Nginx Exporter
  - Prometheus Nginx Log Exporter
- Optional daily MySQL backup worker enabled by the `monitoring` Compose profile
- A separate monitoring-server stack with Prometheus, Grafana, Alertmanager,
  Blackbox Exporter, and a TLS gateway

## Repository layout

```text
app/                     FastAPI routes, middleware, services, and core modules
static/                  Media and admin browser interfaces
nginx/                   Public gateway image and environment configurations
monitoring/              Agents, backups, and the separate monitoring-server stack
tests/                   Unit, configuration, security, UI, and deployment tests
auto_download/           Playlist synchronization and filename-cleaning utilities
docker-compose.yaml      Application stack and host-side collection agents
.env.example             Complete application deployment configuration template
```

Runtime data, credentials, certificates, rendered monitoring configuration, and
database backups are intentionally excluded from Git.

## Requirements

- CentOS 10 or another modern Linux host
- Docker Engine with the Compose v2 plugin (`docker compose`)
- Git
- OpenSSL
- Python 3.12 or newer for host-side tests and monitoring configuration rendering
- Node.js for frontend syntax and smoke checks
- FFmpeg for automatic media synchronization

Run Docker commands with root privileges on hosts where the deployment account
does not have access to the Docker socket. The Compose volume labels already use
`:z` where writable host paths require SELinux relabeling; do not disable SELinux.

## Configuration model

`.env` is the only deployment environment selector. Do not edit
`docker-compose.yaml` or the Nginx source files to switch environments, and do
not rely on an inline shell override for a persistent deployment.

Create the local configuration once:

```bash
cp .env.example .env
chmod 600 .env
```

Then set exactly one of these values in `.env`:

| `ENVIRONMENT` | Public gateway | Intended use |
| --- | --- | --- |
| `development` | HTTP only; no domain or certificate is loaded | Local CentOS development |
| `test` | Production-equivalent TLS, redirects, cookies, and Nginx selection | Staging and pre-production verification |
| `production` | TLS, canonical domain, secure cookies, and strict secret validation | Public deployment |

The Web and Nginx containers both receive the value resolved from `.env`.
Confirm it before every deployment:

```bash
docker compose config --quiet
docker compose config | grep -E 'ENVIRONMENT: (development|test|production)'
```

### Required secrets

Replace every `REPLACE_WITH_...` value in `.env`. Generate independent secrets;
never reuse the application bootstrap token, database passwords, exporter
password, monitoring password, or backup password.

```bash
openssl rand -hex 32
openssl rand -base64 36 | tr -d '\n'
```

`MYSQL_URL` must contain the same password as `MYSQL_PASSWORD`, URL-encoded when
it contains reserved characters. Production startup rejects weak secrets,
insecure admin cookies, mismatched database passwords, and invalid environment
names.

Collection agents are part of the base Compose stack and start in development,
test, and production without an optional profile. `COMPOSE_PROFILES=monitoring`
additionally starts the daily MySQL backup worker. Use that profile in test and
production so staging exercises the production backup path.

## Development deployment

Development mode is intended for a local CentOS 10 workstation without a public
domain or TLS certificate.

1. Prepare writable runtime directories:

   ```bash
   sudo install -d -m 0755 data data/media backups
   sudo chown -R 10001:10001 data
   ```

2. Copy `.env.example` to `.env`, replace every required password/token, and use
   these development-specific values:

   ```dotenv
   ENVIRONMENT=development
   COMPOSE_PROFILES=
   SERVER_NAME=_
   HTTP_PORT=8080
   HTTPS_PORT=8443
   SSL_CERT_PATH=/dev/null
   SSL_KEY_PATH=/dev/null
   WEBRTC_STUN_HOST=localhost
   WEBRTC_STUN_PORT=3478
   WEBRTC_STUN_URLS=stun:localhost:3478
   ADMIN_COOKIE_SECURE=false
   ADMIN_COOKIE_SAMESITE=lax
   ADMIN_COOKIE_NAME=admin_session
   ADMIN_CSRF_COOKIE_NAME=admin_csrf
   ```

   The certificate paths remain defined because Compose validates all bind
   mounts, but development Nginx does not load TLS material.

3. Validate and start the complete development stack:

   ```bash
   sudo docker compose config --quiet
   sudo docker compose up -d --build --wait --wait-timeout 240
   sudo docker compose ps
   ```

4. Verify the runtime:

   ```bash
   curl --fail http://127.0.0.1:8080/api/v1/health
   sudo docker compose exec -T nginx nginx -t
   sudo docker compose exec -T redis redis-cli ping
   ```

Open `http://127.0.0.1:8080/`. The collection agents run on the private Compose
network and do not publish their metric ports on the host.

To include and test the daily backup worker locally, change only this `.env`
line and recreate the stack:

```dotenv
COMPOSE_PROFILES=monitoring
```

```bash
sudo docker compose up -d --wait --wait-timeout 240
```

## Test deployment

Test mode deliberately uses the production Nginx configuration. It must test
TLS redirects, secure cookies, the real collection agents, the MySQL monitoring
accounts, and the backup worker rather than using a reduced development stack.

1. Set these deployment choices in `.env`:

   ```dotenv
   ENVIRONMENT=test
   COMPOSE_PROFILES=monitoring
   SERVER_NAME=test.example.com
   HTTP_PORT=80
   HTTPS_PORT=443
   SSL_CERT_PATH=./certs/fullchain.pem
   SSL_KEY_PATH=./certs/privkey.pem
   ADMIN_COOKIE_SECURE=true
   ADMIN_COOKIE_SAMESITE=strict
   ADMIN_COOKIE_NAME=__Host-admin_session
   ADMIN_CSRF_COOKIE_NAME=__Host-admin_csrf
   ```

2. Install the test certificate under the configured paths. A private staging
   CA is preferred. A short-lived self-signed certificate is sufficient for a
   disposable local CI-equivalent run:

   ```bash
   sudo install -d -m 0700 certs
   sudo openssl req -x509 -nodes -days 1 -newkey rsa:2048 \
     -keyout certs/privkey.pem \
     -out certs/fullchain.pem \
     -subj '/C=CN/ST=Test/L=Test/O=FrontierCloud/CN=localhost'
   ```

3. Start and verify the production-like stack:

   ```bash
   sudo docker compose config --quiet
   sudo docker compose up -d --build --wait --wait-timeout 240
   sudo docker compose logs nginx | grep 'selected production configuration for ENVIRONMENT=test'
   sudo docker compose ps -a mysql_monitoring_init
   sudo docker compose ps mysql_backup
   curl --fail --insecure https://test.example.com/api/v1/health
   ```

Use a hosts-file entry only for an isolated workstation test. A shared test
environment should use real DNS and a trusted test certificate. Keep the same
service topology and resource constraints as production wherever practical.

## Production deployment

### 1. Prepare DNS, firewall, and storage

- Point the application domain to the host.
- Permit TCP 80 and 443.
- Permit TCP and UDP on `WEBRTC_STUN_PORT` when network observation is required.
- Permit metric collection only from the dedicated monitoring server IP.
- Place the TLS full chain and private key outside Git.
- Put `BACKUP_DIR` on storage with enough capacity for seven daily database dumps.

Create the application directories and grant the unprivileged Web UID access to
runtime media:

```bash
sudo install -d -m 0755 data data/media backups certs
sudo chown -R 10001:10001 data
sudo chmod 0700 certs
```

### 2. Configure `.env`

Start from `.env.example`, replace all secrets, and set at least:

```dotenv
ENVIRONMENT=production
COMPOSE_PROFILES=monitoring
SERVER_NAME=media.example.com
HTTP_PORT=80
HTTPS_PORT=443
SSL_CERT_PATH=/absolute/path/to/fullchain.pem
SSL_KEY_PATH=/absolute/path/to/privkey.pem
WEBRTC_STUN_HOST=media.example.com
WEBRTC_STUN_PORT=3478
WEBRTC_STUN_URLS=stun:media.example.com:3478
MONITORING_ALLOW_CIDR=192.0.2.10/32
ADMIN_COOKIE_SECURE=true
ADMIN_COOKIE_SAMESITE=strict
ADMIN_COOKIE_NAME=__Host-admin_session
ADMIN_CSRF_COOKIE_NAME=__Host-admin_csrf
```

`MONITORING_ALLOW_CIDR` must be the monitoring server address, not a broad public
network. `METRICS_BASIC_USER` and `METRICS_BASIC_PASSWORD` must match the values
configured on that server.

### 3. Deploy and verify

```bash
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d --build --wait --wait-timeout 240
sudo docker compose ps
sudo docker compose exec -T nginx nginx -t
curl --fail https://media.example.com/api/v1/health
```

Expected behavior:

- Requests to the configured HTTP domain redirect to HTTPS.
- Unknown hostnames are rejected by the default Nginx servers.
- Web, MySQL, Redis, Nginx, STUN, and every collection agent are running.
- `mysql_monitoring_init` exits successfully after provisioning restricted
  exporter and backup accounts.
- `mysql_backup` remains running when `COMPOSE_PROFILES=monitoring`.
- Exporter ports remain internal; authenticated, allowlisted metric paths are
  exposed only through the application Nginx gateway.

### 4. Update an existing deployment

Inspect incoming changes before rebuilding, especially changes to
`.github/workflows/`, `docker-compose.yaml`, `Dockerfile`, `nginx/`, or
`monitoring/`:

```bash
git fetch origin
git log --oneline --decorate HEAD..origin/main
git diff --stat HEAD..origin/main
git diff HEAD..origin/main -- .env.example
git pull --ff-only
sudo docker compose config --quiet
sudo docker compose up -d --build --wait --wait-timeout 240
sudo docker compose ps
```

Before pulling, reconcile additions or renamed variables shown in `.env.example`
with the host's private `.env`. Git does not update `.env`; Compose validation is
designed to stop before containers are recreated when a required variable is
missing.

Do not run `docker compose down --volumes` during an update; named MySQL and Redis
volumes contain persistent state.

## Separate monitoring server

The `monitoring/` stack is designed for a dedicated host. The application host
runs exporters and backup metrics; the monitoring host scrapes the authenticated
HTTPS endpoints, checks public availability, stores 24 hours of metrics, renders
Grafana dashboards, and sends Telegram alerts.

1. On the monitoring host, create its configuration:

   ```bash
   cd monitoring
   cp .env.example .env
   chmod 600 .env
   ```

2. Set the application hostname, matching metric credentials, monitoring
   hostname, Telegram values, Grafana credentials, and absolute TLS paths.
   Generate the gateway password hash interactively:

   ```bash
   docker run --rm -it --entrypoint htpasswd httpd:2.4-alpine \
     -nBC 12 frontier_observer
   ```

   Keep the bcrypt value single-quoted in `monitoring/.env` so Compose does not
   interpolate its dollar signs.

3. Render root-owned runtime configuration without printing secrets, then start:

   ```bash
   sudo python3 render_config.py
   sudo docker compose config --quiet
   sudo docker compose up -d --wait --wait-timeout 240
   sudo docker compose ps
   ```

The gateway serves `/grafana/`, `/prometheus/`, and `/alertmanager/` over HTTPS.
Only the gateway publishes host ports.

## Admin access and protected API documentation

The Web container issues temporary admin tokens and logs their plaintext value
only at creation time. MySQL stores only token hashes. Follow the token log when
operating directly on the host:

```bash
sudo docker compose logs -f web | grep ADMIN_TOKEN
```

An administrator may explicitly issue a token with the bootstrap secret:

```bash
read -r -s -p 'Bootstrap token: ' bootstrap_token
printf '\n'
curl --fail --request POST \
  --header "X-Token: ${bootstrap_token}" \
  https://media.example.com/api/v1/media/admin/token/issue
unset bootstrap_token
```

Do not place the bootstrap secret in shell history on shared hosts. The admin
browser flow creates an HttpOnly session and requires a separate CSRF header for
state-changing operations.

FastAPI documentation is not public. `/docs`, `/redoc`, and `/openapi.json`
require an existing admin session.

## Validation and CI

Create a local Python environment and run the source checks:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/check_english_comments.py
.venv/bin/python -m unittest discover -v tests
node --check static/js/admin.js
node --check static/js/player.js
node tests/admin_ui_smoke.mjs
```

The English-comment check scans tracked Python comments and docstrings plus
comments in Docker, shell, YAML, TOML, Nginx, JavaScript, CSS, and HTML sources.
Chinese product names, interface text, test data, and other runtime strings are
intentionally allowed.

The GitHub Actions workflow builds the real images with `ENVIRONMENT=test`,
starts the production-like Compose topology, verifies every collection agent and
the backup worker, runs unit and UI flows, validates Nginx, and exercises admin,
media, cache invalidation, and IP-security behavior.

Before pushing, inspect whether the commit changes CI or deployment behavior:

```bash
git diff --name-only origin/main...HEAD
git log --oneline origin/main..HEAD
```

After pushing, wait for the complete workflow result and inspect failed job logs
before considering the deployment ready.

## Automatic media synchronization

The scripts under `auto_download/` synchronize YouTube or Bilibili playlists
directly into `data/media`. `nama_clean.py` is an imported dependency rather than
a standalone copy job: downloaded filenames and playlist directories are cleaned
before they become final media paths. There is no separate `data/clean` stage.

Each source and output type stores a JSON manifest under `data/media/.sync`. The
manifest records the remote media ID, original title, original playlist name,
clean title, and final relative path. A normal run downloads missing items,
reports existing items as skipped, and removes managed local files that no longer
exist in the remote playlist. Back up `data/media` before the first migration run.

Use the project `.venv` as the PyCharm interpreter. Check dependencies without
network access or downloads:

```bash
.venv/bin/python auto_download/yt_mp3.py --check
```

Read remote metadata and preview the synchronization plan without downloading,
deleting, or writing a manifest:

```bash
.venv/bin/python auto_download/yt_mp3.py --dry-run
```

The Windows entry points are `yt_mp3.py`, `yt_m3u8.py`, `bilibili_mp3.py`, and
`bilibili_m3u8.py`. The two `_centos.py` entry points share the same manifests and
logic while resolving FFmpeg and Node.js from the CentOS environment. Routine
yt-dlp chatter is suppressed; normal output contains synchronization stages,
warnings or errors, transfer speed, and the final skipped/added/deleted summary.

## Operations

Useful commands:

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 web nginx
sudo docker compose restart web nginx
sudo docker compose exec -T nginx nginx -t
sudo docker compose exec -T redis redis-cli ping
sudo docker compose exec -T mysql sh -c \
  'mysqladmin ping -h 127.0.0.1 -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" --silent'
```

Stop containers without deleting persistent volumes:

```bash
sudo docker compose down
```

Back up `.env`, TLS material, `data/`, the configured MySQL backup directory, and
the monitoring server's local configuration through a secure system outside Git.
