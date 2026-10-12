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
credentials, host deployment, Docker socket, or product CD.

The `promote-main` guard proves a unique merged same-repository `dev -> main` PR,
successful exact-source `dev` push CI, and identical reviewed/main trees. A newer
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
CA images must never become frozen runnable artifacts.

Every Web fixture has one ASGI worker and one CPU. A timeout is a failure; stop
fixtures before cleaning them. After business work, require CPU below 20% for
five consecutive one-second samples within 30 seconds. STUN probes are serialized.

## Evidence and archival

Keep sanitized results, exact source identity, image IDs/digests, dependency
resolution, Git bundles and restoration proof with the final release. All
required gates in [the acceptance ledger](../python-final-parity.md) must pass
before deleting the two historical Gin refs and archiving. A passing narrow unit
test is never sufficient archival evidence. [Recovery](../operator-recovery.md)
explains frozen images and later unarchival.
