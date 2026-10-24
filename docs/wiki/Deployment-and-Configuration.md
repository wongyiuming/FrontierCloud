# Deployment and configuration

This guide describes the final FastAPI/Python runtime. Docker Compose owns business services and the certificate controller; the product installs no host business systemd service and mounts no Docker socket. Web uses one ASGI worker and one CPU. Additional workers must not bypass process-local media transaction fences.

Use Docker Compose 2.24.4 or later. The MySQL overlay explicitly resets its
optional profile so the documented two-file command activates the database.

## Business nodes

A fresh Standalone node can use the explicit HTTP default without an `.env` file:

```bash
docker compose up -d --build --wait
```

For automatic HTTPS, point DNS at this machine, allow public TCP 80/443, and set:

```dotenv
TLS_ENABLED=true
SERVER_NAME=4399.com
```

Run the normal Compose command. Before issuance, Nginx exposes only ACME challenges; business and Admin never fall back to plaintext. The Python controller issues a trusted certificate, then the Nginx helper validates configuration and enables HTTPS with a graceful reload. Fresh deployments require no existing certificate or Certbot. `TLS_ENABLED` is the only TLS switch. Public HTTP/HTTPS ports are fixed at 80/443. Retired port and certificate-path variables produce an explicit migration error.

SQLite in `./data/frontiercloud.db` is the default database. Redis supplies cache and sessions. Existing MySQL deployments must explicitly retain the MySQL configuration:

```bash
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --wait
```

Do not replace an existing MySQL deployment with the default SQLite database to bypass migration. An existing node vault with a missing SQLite database prevents silent creation of a substitute business database.

## Storage nodes

Storage uses an independent Compose file, not an overlay on business Compose. Direct example:

```dotenv
TLS_ENABLED=true
SERVER_NAME=storage.4399.com
STORAGE_ENDPOINT=https://storage.4399.com
```

```bash
bash scripts/deploy-storage.sh
```

Direct uses native Python HTTPS on public 443. Its isolated certificate agent temporarily binds public 80 during HTTP-01 issuance or renewal and releases it afterward. Storage does not add Nginx. Relay keeps authenticated HTTPS on a restricted high port and has no permanent listener on 80/443.

Native Storage defaults to no trusted reverse proxies. Unlike the business
gateway, it receives HTTPS directly and uses the actual peer address. Only an
explicit `TRUSTED_PROXY_NETWORKS` opts into a separately configured proxy; this
also requires that proxy to supply the validated `X-Real-IP` header.

Relay example:

```dotenv
TLS_ENABLED=true
SERVER_NAME=relay.4399.com
STORAGE_TRANSPORT=relay
STORAGE_PORT=8443
STORAGE_ENDPOINT=https://relay.4399.com:8443
```

Restrict Relay's port to authorized Masters with a firewall. HTTP-01 still requires public 80 during challenges; CA validation does not use 8443. If an AAAA record exists, IPv6 routing and firewall rules must also work. The CA authorization result is the final issuance proof. Direct's non-443 business ports are rejected before startup.

Fresh Storage fixes its role as Follower. Existing role or pairing endpoint mismatches never silently replace identity. The deployment script distinguishes controller liveness, HTTPS readiness, Storage readiness, and pairing readiness. It generates a five-minute single-use pairing package only after actual HTTPS certificate verification. Every Storage has a limited coturn UDP 3478 listener; browser probes run serially.

After business HTTPS is ready, fix its role as Master in Admin, allocate at least 1 GiB of Master Local storage, import the Storage package, and verify active relationships, heartbeat, and Storage desired/observed state. A Follower is not an independent business authority.

## Persistence and recovery

| Content | Location |
|---|---|
| SQLite, media, lyrics, upload recovery state, media premaster keys | DATA_DIRECTORY; defaults ./data or ./storage-data |
| Node vault and Admin/MySQL/metrics secrets | runtime_secrets / storage_secrets |
| ACME account, pending order, cooldown, and lifecycle | certificate_state |
| Complete certificate generations and atomic pointer | certificate_published |
| Local status/download Unix socket | certificate_runtime; recreated on restart |
| Redis persistence | redis_data / storage_redis |
| Optional MySQL data | mysql_data |

Normal `docker compose down` preserves volumes. Do not use `down -v` during upgrades or recovery: it deletes database, identity, and ACME state. All database tables and certificate/control state belong to one recovery set; media follows the operator's separate backup policy.

## Admin and local secrets

Startup logs list newly created secret names without printing values. Web recreation recovers secrets from the persistent `runtime_secrets` volume. In the deployment directory, read the local Admin Key and metrics token:

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

Rapidly click the second half of the home logo five times to open the Admin entry. Submit the local Admin Key there. Do not publish keys in logs, issues, pull requests, or screenshots.

Admin displays certificate status and permits authorized download of the certificate/key pair wrapped for the requesting browser. It offers no upload, certificate paths, or manual renewal. Every file/folder upload requires a new explicit encrypted/plaintext storage choice and site selection. Encryption runs in the browser, never silently falls back after failure, and stores ciphertext on Storage. The backend checks authorization, hashes, and consistency.

`ADMIN_UPLOAD_INACTIVITY_TIMEOUT` defaults to 300 seconds. Authenticated status
passes that same setting to the native browser uploader. Increasing byte
progress renews its watchdog, so a large continuous upload can run longer.
Abort, network loss and inactivity settle the client result; cancellation and
its optional S3 status read share a ten-second control-request budget. Missing
replies leave the authoritative reservation for normal safe reconciliation.
Refresh or closing a tab cannot authorize deleting existing published media.

Online upgrade/rollback business APIs, UI, and Updater have been retired.
The candidate [Compose/Python operator](../python-source-operator.md) provides
host one-shot installation, upgrading and controlled state recovery; it is
subject to the current renewal's release gates. The earlier
[operator recovery](../operator-recovery.md) and
[automatic Python TLS](../automatic-tls-python.md) retain legacy recovery and
certificate-migration guidance.
