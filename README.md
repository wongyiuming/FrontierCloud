# FrontierCloud

FrontierCloud is a FastAPI media service deployed with Docker Compose. The
business stack includes Nginx, MySQL, Redis, a STUN-only coturn service, and
resource-bounded exporters. A separate monitoring stack provides Prometheus,
Grafana, Alertmanager, and Blackbox Exporter.

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

Replace every placeholder in `.env` with an independent secret. The password
inside `MYSQL_URL` must be URL-encoded and must match `MYSQL_PASSWORD`.

Use these environment values:

- `ENVIRONMENT=development` for the private HTTP deployment.
- `ENVIRONMENT=test` for RN. This selects the production Nginx and TLS path.
- `ENVIRONMENT=production` for DMIT.

Prepare persistent paths and validate the deployment:

```bash
sudo install -d -m 0755 data data/media backups certs certs/acme
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

## Monitoring deployment

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

## Administrator access

The web service periodically issues short-lived administrator tokens. Read the
current token from the web container log:

```bash
sudo docker compose logs -f web | grep ADMIN_TOKEN
```

Never place deployment secrets in commands, documentation, source files, or
Git history.
