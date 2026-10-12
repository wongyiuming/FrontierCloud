# Operations and troubleshooting

Preserve logs, image identities, and state first. Distinguish the entry point, business process, database, Redis, certificate controller, heartbeat, and Storage transactions. A failed backup does not prove that a healthy node is offline.

```bash
docker compose ps
docker compose logs --tail=200 web certificate-agent nginx redis
git rev-parse HEAD
git status --short
df -h
docker system df
```

Storage commands add `-f docker-compose.storage.yaml`; Storage has no Nginx service. Existing MySQL deployments must use their saved Compose combination and inspect MySQL logs. Changing database type or clearing tables is not a migration strategy.

## Certificates and startup

```bash
docker compose exec -T web python -m app.services.certmanager.probe
docker compose exec -T web python -m app.services.certmanager.probe --https-ready
docker compose exec -T web python -c 'import asyncio,json; from app.services.certmanager import certificate_control; print(json.dumps(asyncio.run(certificate_control()),indent=2))'
curl -fsS https://4399.com/health/ready
```

The default probe checks controller liveness. `--https-ready` also requires a valid certificate, an actual verified HTTPS handshake, and matching managed/served fingerprints. FAILED/PENDING controllers can remain live and retry with backoff; liveness alone is not a successful deployment.

| State or error | Investigation |
|---|---|
| dns_unavailable | Check SERVER_NAME, A/AAAA records, and DNS propagation |
| port80_unavailable | Check the existing port 80 listener; Storage borrows it only during challenges |
| challenge_unreachable | Check public 80, firewall, proxy/CDN challenge routing |
| caa_denied | Check CAA records and the CA authorization response |
| ca_rate_limited | Preserve cooldown state and wait; repeatedly recreating volumes defeats rate-limit protection |
| certificate_storage_unavailable | Check disk space, read-only mounts, and controlled directory permissions |
| serving_certificate_mismatch | Check Nginx reload, the actual SNI entry point, and proxy termination |
| certificate_expired | Restore issuance prerequisites; never disable verification |
| clock_unreliable | Correct the system clock and allow automatic recovery |

Failed issuance preserves a valid old certificate. Nginx validation, reload, or handshake failures preserve or restore the previous complete configuration. Storage loads a complete new SSL context for new connections; rotation does not terminate existing media connections. A missing certificate leaves restricted HTTP bootstrap only; no self-signed certificate or plaintext business fallback is generated.

Follow [certificate migration](../automatic-tls-python.md) to protect and adopt valid legacy material before removing retired `.env` fields. Do not edit internal certificate generations or account keys in production. DNS-01 is not implemented; environments unable to satisfy public HTTP-01 report a failure.

## Uploads, media, and node consistency

An interrupted browser upload is recovered through expiry checks and upload/write leases. Do not delete quota or lease records belonging to valid active uploads. Check Admin upload state, member health/writability/capacity, and recovery logs. Direct SQL deletion of reservations, moving files, or clearing temporary directories must not manufacture success.

Use Admin for rename, delete/restore, hiding, and priority changes. Each complete object belongs to one Storage member. Directory bindings, offline members, pending deletes, or active uploads can correctly block conflicting operations. Encrypted Range responses contain ciphertext; the browser decrypts them. Backend plaintext conversion must not compensate for browser errors.

Pairing requires trusted HTTPS and signed identities on both nodes. Regenerate expired or consumed packages instead of disabling verification or modifying roles/relationship keys. The Storage script starts the five-minute pairing window only after HTTPS readiness.

Every Storage exposes restricted UDP 3478 STUN. Browser probes run one node at a time. Concurrent polling or extra server computation must not compensate for proxy GeoIP filtering.

## Maintenance, recovery, and reclamation

The ordinary Admin maintenance switch differs from the offline operator's `data/.frontiercloud-native-maintenance` hard fence. The hard fence takes precedence, cannot be reopened through GUI, and accepts no historical force-open override.

Before upgrading, stop writes and back up all database tables, secrets, media premaster keys, and ACME state/published volumes. Media follows the existing independent backup. Copying a live SQLite file or restoring only the database breaks the recovery boundary. Follow [operator backup and recovery](../operator-recovery.md).

Before cleanup, inspect disk usage and actual container references. Preserve running images, explicit rollback images, valid recovery sets, data, and ACME volumes. Do not run broad `docker system prune -a --volumes` on a shared host or treat `down -v` as routine reclamation.

The product has no online Updater or upgrade GUI. Missing retired update APIs are intentional and must not be resurrected from historical scripts. Restore the archived release with frozen images/source and a complete recovery set.
