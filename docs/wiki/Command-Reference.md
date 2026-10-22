# Command reference

Run Compose commands in the deployment directory with that deployment's saved `.env` and Compose combination. Example domains use `4399.com`; replace them with your own domain. SQLite is the default and Web uses one worker. The product has no host business systemd or online Updater commands.

## Startup and status

```bash
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail=200 web certificate-agent nginx redis
curl -fsS https://4399.com/health/live
curl -fsS https://4399.com/health/ready
```

Explicit Standalone HTTP mode uses `http://127.0.0.1/health/ready`. TLS checks must not use `curl -k` or disable certificate verification.

Existing MySQL configuration:

```bash
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --wait
```

Initial Storage deployment and a new five-minute single-use pairing package:

```bash
bash scripts/deploy-storage.sh
docker compose -f docker-compose.storage.yaml ps
docker compose -f docker-compose.storage.yaml exec -T web python -m app.services.certmanager.pair
```

The final command succeeds only after actual HTTPS readiness and before pairing. Its output includes a one-time credential; deliver it only to the authorized Master.

## Automatic TLS

```bash
docker compose exec -T web python -m app.services.certmanager.probe
docker compose exec -T web python -m app.services.certmanager.probe --https-ready
docker compose exec -T web python -c 'import asyncio,json; from app.services.certmanager import certificate_control; print(json.dumps(asyncio.run(certificate_control()),indent=2))'
```

Storage commands add `-f docker-compose.storage.yaml`. Status does not return private keys. Authorized Admin download returns a browser-wrapped certificate/key pair. There is no upload, path configuration, manual renewal, or verification-disable API.

Direct uses public 443 and temporary challenge 80. Relay uses a restricted internal high port such as 8443 and borrows public 80 only during HTTP-01; the CA does not validate HTTP-01 on 8443. See [automatic TLS migration](../automatic-tls-python.md) for one-time offline adoption of a trusted legacy pair.

## Locally generated secrets

```bash
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'
docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'
```

Do not copy these values into public logs, issues, pull requests, or screenshots. Redis Admin sessions are not a complete business recovery set. Protect node vault, database, media premaster keys, and certificate state together.

## Read-only database observations

These SQL queries inspect default SQLite without exposing stored credentials. See [operator recovery](../operator-recovery.md) for connection and backup procedures. Optional MySQL uses the same logical tables and columns. Do not delete or replace database files for maintenance.

```sql
SELECT singleton,node_id,role,endpoint FROM node_identity;
SELECT generation FROM frontiercloud_schema WHERE singleton=1;
SELECT generation,migration_name,checksum,applied_at
FROM frontiercloud_schema_migrations ORDER BY generation;
SELECT peer_id,peer_endpoint,mode,state,status,last_heartbeat
FROM node_relationships ORDER BY peer_endpoint;
SELECT member_id,transport,health,writable,allocated_bytes,used_bytes,reserved_bytes
FROM cluster_storage_members ORDER BY member_id;
SELECT media_path,storage_member_id,state,size_bytes
FROM global_media_objects ORDER BY media_path;
SELECT upload_id,state,created_at,updated_at,expires_at
FROM cluster_upload_sessions ORDER BY updated_at DESC LIMIT 100;
SELECT media_path,preference FROM media_playback_stats ORDER BY preference DESC,media_path;
```

Priorities range from `-7..500000` with deterministic ties. Drag reorder sends a complete scope revision and executes in a backend transaction. Manual SQL must not bypass business fences.

## Images, disk, and development validation

```bash
docker compose images
docker system df
df -h
git rev-parse HEAD
git status --short
python -m unittest discover -s tests -p 'test_*.py' -v
bash scripts/test-python-certificate-nginx.sh
bash scripts/test-python-certificate-storage.sh
```

Certificate integration scripts use isolated development-host test containers with a local CA and normal system trust. They do not contact the production CA or publish host ports. A release requires bounded source CI, complete development-host native acceptance and review of the same final dev commit.

Normal `docker compose down` retains volumes. Do not use `down -v`, clear data, delete upload leases, manually advance schema generation, or globally prune volumes as troubleshooting steps. Follow [operator recovery](../operator-recovery.md) for offline upgrade and complete restore.
