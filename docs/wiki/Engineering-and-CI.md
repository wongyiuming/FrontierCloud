# Engineering and CI

`dev` is the only implementation branch; `main` accepts exactly one
same-repository `dev -> main` release PR. Do not create other branches, force-push,
or implement directly on `main`. After merge, fast-forward `dev` to `main`.
The owner reviews/authorizes release promotion.

The final runtime is Python/FastAPI, SQLAlchemy async, SQLite by default or the
original optional MySQL, Redis, native browser JavaScript and Compose. Business
HTTPS uses Nginx. Native Storage terminates TLS in Python. There is no Go backend,
privileged online updater, Python media encryption worker, or Compute Worker.

Read the authoritative [architecture boundaries](../../ARCHITECTURE.md) and
[contribution procedure](../../CONTRIBUTING.md) before changing cross-cutting code.

## Source CI

The read-only `docker.yml` workflow installs pinned Python dependencies, checks
Python/JavaScript syntax, runtime CPU boundaries, deployment/env contracts and
English source comments, discovers the complete unit suite, and runs the browser
JavaScript smoke tests. Source jobs have a three-minute limit. CI owns no SSH
credentials, deployment authority, or product CD. Product containers never
receive a Docker socket or host service control.

The required `test-compose` check depends on successful source CI and runs the
actual `scripts/test-python-final.sh` suite on a fresh GitHub-hosted Linux Docker
runner, with a fifteen-minute limit. It runs for `dev` pushes, release PRs and
manual workflow dispatches. Both test jobs check out the exact PR head SHA or
event SHA with persisted Git credentials disabled. Test-only pinned browser
dependencies, NSS tools and the runner's verified Chrome support the fixtures;
no deployment credentials or production machine is involved.

The `promote-main` guard proves a unique merged same-repository `dev -> main` PR,
successful exact-source `dev` push CI including both `source` and `test-compose`,
and identical reviewed/main trees. A `main` push reuses that proven tree rather
than repeating source or native builds. Missing, skipped or failed required jobs
cannot satisfy promotion. A newer
commit invalidates earlier CI and native acceptance evidence.

## Complete native acceptance

Run `scripts/test-python-final.sh` from a clean exact source checkout on an
isolated Linux Docker host. `FC_EVIDENCE_DIR` is an absolute private evidence
folder outside the checkout; `FC_BROWSER_PYTHON` selects an installed test-only
Python environment containing pinned httpx/Playwright and Chromium.

The suite builds actual product Dockerfiles, runs unit discovery inside a
single-core disposable container, checks source and browser JavaScript, then
runs real ACME/Nginx and native Direct/Relay TLS fixtures and the three-node
Chromium federation harness. The same gate runs real optional-MySQL migrations,
cold database/control backup and new-project restoration, and the actual offline
identity-preserving endpoint wrapper for both databases.

Fixtures use a private local ACME CA, actual HTTP-01/JWS/CSR/account operations
and test-only trust roots. They do not contact a public CA, publish host HTTP/HTTPS
ports, disable certificate checking, or affect existing deployments. Test-only
CA images must never become product deployment images.

CI uploads only explicitly selected sanitized JSON receipts, the source commit
receipt and test logs. Private CA directories, NSS profiles, database/control
snapshots, certificate pairs and runtime keys are excluded. The artifact collector
rejects private-key PEM material; it never uploads the fixture or deployment
directories. These workflow artifacts expire after 14 days and are not a permanent
evidence archive. A durable summary belongs in the existing acceptance ledger.
Independent exact-source native evidence remains required for the
final archival deliverable.

Every Web fixture has one ASGI worker and one CPU. A timeout is a failure; stop
fixtures before cleaning them. After business work, require CPU below 20% for
five consecutive one-second samples within 30 seconds. STUN probes are serialized.

## Evidence and archival

Keep sanitized results and exact source identity with the CI run. All
required gates in [the acceptance ledger](../python-final-parity.md) must pass
before deleting the two historical Gin refs and archiving. A passing narrow unit
test is never sufficient archival evidence. Retain only Python `main` and `dev`;
do not preserve Gin branch recovery references or bundles. GitHub's native archive
operation makes the repository read-only and can be reversed in Settings.
[Recovery](../operator-recovery.md) explains private deployment backup and restore.
