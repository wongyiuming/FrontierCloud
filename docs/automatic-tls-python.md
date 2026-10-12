# Automatic TLS for the Python final release

The Python implementation uses `python-acme` to handle ACME v2 and the
`cryptography` standard certificate verifier. It does not run Go, Certbot,
host systemd services, or a business container with the Docker socket.

Fresh business deployment only needs DNS pointing to its public address and:

```dotenv
TLS_ENABLED=true
SERVER_NAME=4399.com
```

Run `docker compose up -d --build`. Public TCP 80 must be reachable for HTTP-01
and public HTTPS is always TCP 443. HTTP bootstrap serves only ACME requests;
it does not expose business APIs or Admin before a trusted certificate exists.
Explicit `TLS_ENABLED=false` retains standalone HTTP semantics. A fixed Master
or Follower cannot run without verified TLS. `HTTP_PORT` and `HTTPS_PORT` are
retired; any nonempty legacy port value is rejected and Compose publishes only 80/443.

Storage uses `docker-compose.storage.yaml` and `scripts/deploy-storage.sh`.
Direct storage serves native Python HTTPS on public 443. Relay uses its
restricted configured high port (default example 8443), and serves no ordinary
business listener on public 80/443. The isolated host-network certificate-agent
temporarily binds public 80 during an ACME challenge and releases it in a
`finally` block. Port conflicts do not kill Storage; they are reported and retried
with backoff. The certificate controller remains live while TLS is pending.
Compose health distinguishes controller liveness from HTTPS/business readiness.
No pairing package is generated before real HTTPS verification; its five-minute
single-consumption lifetime starts after that verification.

Business Nginx continues to terminate HTTPS. Its Python edge helper can execute
only its own `nginx -t` / `nginx -s reload`, under a local reload lock. It validates
the issuer's protected publication receipt, loads the complete pair, validates
Nginx configuration, reloads, and verifies the real local TLS handshake. Failure
restores the old configuration and serving generation. Storage selects complete
SSL contexts for new handshakes; existing streams retain their connection.

Before opening an ACME account or order, the Business controller checks an exact
random challenge through the fixed Compose-local `nginx:80` route with the
configured domain as Host. An unavailable route, redirect or unexpected body
keeps bootstrap PENDING and retries after five seconds without consuming a CA
authorization or increasing the CA failure backoff. The real challenge route
is checked again before answering the CA. Local readiness is only a startup
guard: issuance still requires the CA's actual public HTTP-01 validation.
An unready renewal route retains the valid serving generation and reports
DEGRADED until the route recovers.

Three named volumes are part of the recovery boundary:

- `certificate_state`: private ACME account key/registration, pending order,
  lifecycle journal and exclusive controller lease; only the agent mounts it.
- `certificate_published`: immutable complete pair generations and an atomically
  replaced pointer. Nginx or Storage mounts it read-only. Never remove this
  volume with `docker compose down -v` during upgrades.
- `certificate_runtime`: restricted Unix status/download socket, not a public
  management port. Business Web cannot read the account/private cache.

Status states are DISABLED, PENDING, VALIDATING, ISSUING, ACTIVE, RENEWING,
DEGRADED and FAILED. ACTIVE requires an unexpired trusted managed pair and an
actual public HTTPS handshake with its fingerprint. Issuance/renewal failures
retain valid old material. Retry cooldown and incomplete orders survive process
restarts. ACME Renewal Information is used when the CA supports it, with a
conservative lifetime-based fallback. Domain changes invalidate serving readiness
and request a new identity. Private keys are never logged or included in status.

Admin displays read-only certificate facts. Authorized certificate/key downloads
are wrapped to the requesting browser's ephemeral P-256 key using
HKDF-SHA256/AES-256-GCM. Remote download envelopes travel through existing
authenticated node communication; the Master does not receive Storage plaintext
private keys. There is no certificate upload/path/manual renew API.

## Upgrade existing external certificates

This release must not be installed by just deleting old certificate mounts. First
stop writers in a maintenance window, back up **all database tables**, persistent
node identities/secrets and certificate state, and retain the previous Compose
configuration/images for rollback. Media backup policy remains operator-owned.

For the former external certificate deployment, prepare a one-time read-only
import mount at the fixed internal `/run/frontiercloud-certificate-import` with
`fullchain.pem` and `privkey.pem`. Start certificate-init, then run the isolated
`python -m app.services.certmanager.migrate` command with the same three managed
volumes and that import mount. The migration takes the exclusive issuer lease,
verifies public trust/SAN/time/key matching, refuses to overwrite another valid
managed identity, and commits the pair atomically. This command is offline
migration tooling, not a runtime user-configurable certificate path or API.

Only after the import succeeds remove `SSL_CERT_PATH`, `SSL_KEY_PATH`,
`STORAGE_TLS_CERT`, `STORAGE_TLS_KEY`, and `ACME_WEBROOT` from the old `.env`.
Their presence gives a clear migration error. Start the new Compose and verify
ACTIVE plus `serving_matches=true` before ending maintenance. If the old pair is
invalid or untrusted, retain the rollback stack and allow automatic issuance in
bootstrap; never enable plaintext business traffic or disable TLS verification.

Restore the entire agent state and published generations together to preserve
an existing ACME account and cooldown. Across machines the domain must still
resolve to the new verified ingress; restore does not bypass CA validation on
future renewal. The migration intentionally does not import arbitrary Certbot
account layouts. An imported valid serving certificate can be renewed under a
new product-owned ACME account without exposing external account secrets.

Unit tests use an explicitly injected local CA inside test code. Production has
no test-CA URL, custom-root path, self-signed fallback, or insecure verification
switch. Test-only trust roots must never be used in release images.
