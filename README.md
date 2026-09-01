# FrontierCloud

FrontierCloud is a FastAPI media service deployed with Docker Compose. The
business stack includes Nginx, MySQL, Redis, and a STUN-only coturn service.
Resource-bounded collectors are opt-in, and a separate monitoring stack
provides Prometheus, Grafana, Alertmanager, and Blackbox Exporter.

## Environment model

The repository uses two long-lived branches:

- `dev` supplies both the private development host (`192.168.6.x`) and the RN
  preproduction host.
- `main` supplies the DMIT production host.

Private development is HTTP-only and proves functional correctness. NAT444
means it cannot prove public routing, certificates, DNS, or internet-facing
behavior.

RN is a production-shaped deployment. It must use the same public ports,
TLS behavior, container topology, security controls, and operational checks as
DMIT. A release candidate must pass business validation on RN and remain stable
for more than 36 hours before a `dev` to `main` pull request is opened.

Only stable, meaningful releases and critical production fixes belong in a
promotion pull request. The repository owner decides whether to merge. DMIT is
updated from `main` only after that merge.

Use focused commits while developing, then push a coherent batch after the
candidate reaches a useful checkpoint. Do not open a release pull request for
incidental edits.

## Language policy

Comments, docstrings, explanatory documents, and operational documentation are
written in native US English. User-facing product copy may remain localized.
Run the policy check before committing:

```bash
python scripts/check_english_comments.py
```

## Business deployment

Requirements: Git, Docker Engine, and Docker Compose v2. Keep SELinux enabled
on production hosts.

```bash
git clone https://github.com/wongyiuming/FrontierCloud.git
cd FrontierCloud
cp .env.example .env
chmod 600 .env
```

Replace every placeholder in `.env` with an independent secret. Required values
stay at the top of each example file. Supported optional overrides are commented
at the bottom; copy or uncomment only the values that the deployment must change.
Defaults belong to the application and Compose files, not to the active `.env`.

The root `.env.example` is the authoritative FrontierCloud configuration
contract. It lists every supported application and business-stack setting,
enables only values without a safe default, and excludes hostnames, addresses,
credentials, and topology that belong to an operator's monitoring or deployment
environment. Changes to a supported setting must update configuration code,
this template, documentation, and contract tests in the same commit.

`MYSQL_URL` is generated from `MYSQL_USER`, `MYSQL_PASSWORD`, and
`MYSQL_DATABASE`. Set the optional `MYSQL_URL` override only for a nonstandard
database endpoint; its password must be URL-encoded and match `MYSQL_PASSWORD`.

Transport behavior is selected directly instead of being inferred from an
environment name:

- Set `TLS_ENABLED=false` for the private HTTP deployment.
- Keep the default `TLS_ENABLED=true` for RN and DMIT HTTPS deployments.

The same secret-strength checks apply in both modes. Cookie security and names
are derived from `TLS_ENABLED`, so HTTP deployments use unprefixed non-secure
cookies while HTTPS deployments use secure `__Host-` cookies.

Prepare persistent paths and validate the deployment:

```bash
sudo install -d -m 0755 data data/media data/media/music data/media/vido backups certs certs/acme
sudo chown -R 10001:10001 data
sudo chmod 0700 certs
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d --build --wait --wait-timeout 240
sudo docker compose ps
sudo docker compose exec -T nginx nginx -t
```

The public Nginx service owns host ports 80 and 443 on RN and DMIT. The
production configuration redirects HTTP to HTTPS, rejects unknown hostnames,
and serves ACME HTTP-01 files from `ACME_WEBROOT` before redirecting other
requests.

Never run `docker compose down --volumes` on a persistent deployment.

## Updating a host

Review the incoming change and any new environment variables before updating:

```bash
git fetch origin
git log --oneline HEAD..origin/main
git diff --stat HEAD..origin/main
git diff HEAD..origin/main -- .env.example monitoring/.env.example
git pull --ff-only
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d --build --wait --wait-timeout 240
sudo docker compose ps
```

RN follows `origin/dev`; DMIT follows `origin/main`.

RN normally updates through the `deploy-rn` job after a `dev` push passes the
complete `test-compose` job. The deployment job has three independent guards:
the push event, the exact `refs/heads/dev` ref, and the `rn-preproduction`
environment branch policy. A `main` push can run CI but cannot enter CD.

## Monitoring deployment

Monitoring, ELK, reporting, concrete target hosts, and their credentials are
operator infrastructure rather than standard FrontierCloud configuration. They
remain in the independent `monitoring/` deployment and its private runtime
files; they must not be added to the root `.env.example`. Per-instance collector
sidecars are enabled explicitly with the `monitoring` Compose profile.

The monitoring stack does not own the standard public web ports. Its defaults
are HTTP 8080 and HTTPS 8443 so the RN business deployment can use 80 and 443.
Set `MONITORING_HTTP_PORT` and `MONITORING_HTTPS_PORT` explicitly in
`monitoring/.env` when host policy requires different dedicated ports.

```bash
cd monitoring
cp .env.example .env
chmod 600 .env
python render_config.py
docker compose config --quiet
docker compose up -d --wait
```

With the defaults, Grafana remains available at
`https://MONITORING_SERVER_NAME:8443/grafana/`. Prometheus and Alertmanager use
the corresponding `/prometheus/` and `/alertmanager/` paths.

RN owns two isolated monitoring Compose projects. Project `monitoring` observes
production and retains eight days for weekly aggregation. Project
`frontiercloud-rn-self-monitoring` uses `monitoring/rn-self.env`, separate
runtime files, credentials, ports 9080/9443, networks, and data volumes to
observe RN itself. `scripts/deploy_rn.sh` renders and updates both projects.

The `reporting` profile exists only in the production-observer project. It
incrementally reads the two bounded Nginx logs, queries the already collected
Prometheus series and weekly ban summaries, and sends a Monday 09:00
Asia/Shanghai report to Telegram. IP-to-city processing runs locally on RN
with the monthly DB-IP City Lite database; no client address is sent to a
geolocation API.

## Standalone download tools

`auto_download/` is a collection of operator-owned tools stored in this
repository for history and convenience. It is not application code, is not
copied into the business image, is not exposed by the FastAPI router, and is not
part of CI or the business test suite.

Install tool-only dependencies on an operator workstation when needed:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[tools]"
```

Tool behavior and release cadence are independent from the business service.

Audio and video storage are separated by ownership, not inferred from filename
extensions. Audio belongs under `data/media/music` and video under
`data/media/vido`. Each media type supports either files directly in a category
or files in one additional child folder, but never both layouts in the same
category. Deeper media paths are intentionally ignored by the public catalog.

Before the first deployment of this layout, inspect and apply the legacy data
migration from the application image:

```bash
sudo docker compose run --rm web python -m app.services.migrate_media_layout
sudo docker compose run --rm web python -m app.services.migrate_media_layout --apply
```

## Administrator access

The web service periodically issues short-lived administrator tokens. Read the
current token from the web container log:

```bash
sudo docker compose logs -f web | grep ADMIN_TOKEN
```

Never place deployment secrets in commands, documentation, source files, or
Git history.
