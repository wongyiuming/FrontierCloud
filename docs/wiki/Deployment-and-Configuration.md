# Deployment and Configuration

## 1. Prerequisites

Recommended host environment:

- Linux;
- Docker Engine;
- Docker Compose v2;
- valid DNS/certificate conditions when HTTPS is enabled;
- enough disk for `./data`, MySQL, Redis, images, and recovery data.

## 2. Minimal startup

Local HTTP testing requires no `.env` file:

```bash
docker compose up -d --build --wait
```

Check services:

```bash
docker compose ps
```

Follow logs:

```bash
docker compose logs -f --tail=200
```

## 3. HTTPS

Production fixed Master/Follower roles should use HTTPS.

Create `.env`:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
```

Default certificate paths:

```text
certs/fullchain.pem
certs/privkey.pem
```

Optional overrides:

```dotenv
SSL_CERT_PATH=./certs/fullchain.pem
SSL_KEY_PATH=./certs/privkey.pem
```

The certificate must match `SERVER_NAME`.

## 4. Compose services

```text
secrets-init
media-init
updater
web
redis
mysql
nginx
stun
```

### secrets-init

Initializes persistent runtime secrets, including:

- MySQL application password;
- MySQL root password;
- Admin Key;
- metrics token.

The persistent secret volume is:

```text
runtime_secrets
```

### media-init

Initializes host media directories and permissions under:

```text
./data
```

### web

Runs FastAPI. The normal Web process is unprivileged, uses a read-only root filesystem, and receives only required writable mounts.

### updater

Controls local release build/replace, upgrade, rollback, and cluster release coordination.

### mysql / redis

MySQL stores durable business facts. Redis provides runtime cache/coordination.

### nginx

Public HTTP/HTTPS entry point for TLS, proxying, static resources, maintenance behavior, and edge enforcement.

### stun

Provides STUN for mandatory WebRTC observation.

## 5. Persistent data

Important persistent locations:

```text
./data                 media, lyrics, recordings, and related files
mysql_data             MySQL data
redis_data             Redis data
runtime_secrets        Admin/MySQL/metrics secrets
updater_control        Web-to-Updater control channel
maintenance_state      maintenance state
```

Normal shutdown:

```bash
docker compose down
```

preserves volumes.

This is destructive:

```bash
docker compose down --volumes
```

It removes database/secret volumes and is not a normal upgrade procedure.

## 6. Common environment settings

See `.env.example` for the complete supported contract. Common settings include:

```dotenv
TLS_ENABLED=true
SERVER_NAME=media.example.com
INSTANCE_NAME=frontiercloud
LOG_LEVEL=INFO
LOG_FORMAT=json
MYSQL_DATABASE=office_automation
MYSQL_USER=media_admin
WEBRTC_STUN_PORT=3478
PUBLIC_BIND_ADDRESS=0.0.0.0
```

### GitHub release-verification token

The Master supports an optional read-only GitHub token:

```dotenv
GITHUB_API_TOKEN=...
```

It is used only server-side for release-verification REST requests and greatly increases the available request quota compared with anonymous GitHub REST access.

FrontierCloud also caches verification and applies rate-limit backoff, but a least-privilege read-only token is still recommended for production.

## 7. Reading generated secrets

Admin Key:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
```

Metrics token:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

MySQL application password:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/mysql_password'
```

Do not paste these values into public logs, issues, PRs, or screenshots.

## 8. Health and metrics

Liveness:

```text
/health/live
```

Readiness:

```text
/health/ready
/health
```

Prometheus-compatible metrics:

```text
/metrics
```

The metrics endpoint requires the generated Bearer token.

## 9. Logs

Services log to stdout/stderr, with Docker `json-file` rotation limits.

```bash
docker compose logs -f web
docker compose logs -f updater
docker compose logs -f mysql
docker compose logs -f nginx
```

Production environments should forward logs to an external collector if required. FrontierCloud does not currently deploy a logging/monitoring platform on its own.

## 10. IPv4 and IPv6

Public ports default to:

```dotenv
PUBLIC_BIND_ADDRESS=0.0.0.0
```

Use:

```dotenv
PUBLIC_BIND_ADDRESS=::
```

only when the host/network are intentionally prepared to publish IPv6.

## 11. Recommended first deployment

```text
1. Clone/checkout production main
2. Prepare .env
3. Prepare HTTPS certificate for production
4. docker compose up -d --build --wait
5. Verify /health/ready
6. Read the Admin Key
7. Sign in to Admin
8. Fix Master/Follower roles
9. Pair Followers
10. Configure Storage / Compute / Backup
11. Verify Desired == Observed
12. Verify upload, playback, and node state
```

## 12. Do not use these as upgrade shortcuts

Do not:

- manually drop MySQL tables;
- manually edit Schema Generation;
- delete managed media directly;
- bypass pending-delete/recovery state by removing directories;
- use `docker compose down --volumes` for an ordinary upgrade;
- operate a Follower as a second independent business Master.

Production upgrades should use the system release-management path and release gate.
