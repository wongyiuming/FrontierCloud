# Python final release and archival acceptance

The owner authorized this final parity release on 2026-10-12. The reference is
FrontierCloud-Gin main `a6f8e3a5ec960578bc0d08b070afbddc2370243f`, tree
`c3752ce3d41ee4183e5433a08a4feea0a84ac7fd`. Implementation remains FastAPI/Python;
neither a Go business process nor a forwarding wrapper around Gin is acceptable.
Only existing `dev` and `main` are implementation/release branches.

## Required acceptance ledger

This ledger defines required evidence, not a passing declaration. The final
release must have sanitized results and a source-commit receipt for the
exact tested source, with the corresponding CI logs below. All gates must pass
before archival; historical component results alone do not authorize it.

| Area | Required behavior | Final evidence |
| --- | --- | --- |
| Persistence | Default SQLite, optional original MySQL, marked resumable migrations, unchanged historical business values | unit.log + mysql.log |
| Media encryption | Explicit plaintext/encrypted choice on each selection; browser AES-GCM chunks; no plaintext fallback | federation.json |
| Keys | Session-scoped asymmetric temporary authorization; persistent premaster; fixed expiry and authenticated key wrapping | federation.json + unit.log |
| Media lifecycle | Local, Direct, Relay upload, storage, Range, playback, download, rename, delete and recovery | federation.json + unit.log |
| Upload recovery | Safe expired reservation reclamation; active byte writers never released | unit.log + federation.json |
| Placement | Immediate-parent affinity and durable reservation consistency | unit.log + federation.json |
| Priority | -7..500000; exact old values; stable ties; atomic revision-checked same-parent drag | federation.json + unit.log |
| UI | Current Admin layout, smooth mouse/touch/keyboard drag, accessibility and responsive views | federation.json |
| Network | All storage STUN endpoints; strictly serial probing | federation.json |
| Automatic TLS | Python-managed ACME accounts/cache, first issuance, renewal, retention on failure, served-certificate proof | nginx-real.json + storage-direct-real.json + storage-relay-real.json |
| Entry points | Nginx Business ingress; Python Storage TLS; Direct 80/443, Relay private high port | storage-direct-real.json + storage-relay-real.json |
| Certificate UI | Read-only status and authorized download only; no path, upload or renew controls | federation.json |
| Configuration | Automatic TLS two-value fresh baseline; retired path/port settings rejected with migration diagnosis | unit.log + source policy checks |
| Upgrades | Online upgrade/rollback APIs, UI and privileged updater removed; safe operator migrations retained | unit.log + operator-endpoint.log |
| Security | Nonce CSP, HTTPS trust, authenticated node control, exact media ownership and bounded compute | unit.log + federation.json |
| Recovery | Container restart/recreate, private database/control restoration and normal GitHub unarchive | operator-backup.log + source deployment instructions |

## Archival order

1. Implement and test on `dev`, using isolated disposable development deployments.
2. Pass exact-source CI and real Python Business/Direct/Relay acceptance.
3. Promote through one same-repository `dev -> main` PR, then fast-forward `dev`.
4. Verify GitHub `main` is the tested runnable Python implementation.
5. Delete `gin_dev` and `gin_main`; retain no Gin archive refs or recovery bundles.
6. Archive using GitHub's native operation and verify read-only status and Python branches.

Production deployments are outside this task. Existing media and production nodes
must not be used as test fixtures. Test CA trust is confined to disposable test
images; production CA URLs, TLS validation and public 80/443 restrictions remain
enforced. Python and Gin database generations are repository-owned migration
contracts: equal generation numbers alone do not authorize interchanging their
databases, node vaults or private keys.

## Deliberate retirement and replacement coverage

The reference release removed online software replacement. The Python
release follows that contract: no Web upgrade/rollback endpoints, no cluster
software-distribution endpoints, no Admin release module, and no privileged
Docker-socket updater. Existing authenticated backup, media-control and node
identity routes remain registered through `app/api/internal_control.py`.

37 cases in the four former online-updater suites are retired with those removed
product components, rather than skipped to hide a failure. Replacement protection is
`tests/test_retired_release_boundary.py` (actual HTTP 404 and operator-fence
precedence), deployment/source policy assertions against Compose socket/mounts,
and the remaining Admin/backup/worker-retirement regression suites. Manual
migration and private deployment recovery receive independent acceptance.

The old mixed federation/security/site-control suites replace assertions for the
removed updater with actual authenticated control routing and hard maintenance
fences. The historical ten-node updater/CD harness is retired; current native
acceptance exercises actual Business, Direct and Relay deployments with the
current TLS controller and browser contracts. Python source history remains in
the repository. GitHub workflow artifacts contain sanitized receipts and logs
for 14 days, then expire; they are not a permanent archival evidence store.
The durable functional acceptance summary is recorded below in this document.

## Completed functional acceptance

The Python implementation was accepted at source
`ce6c539523a1cad4dfdd6e7c56c215704ff210cf`, tree
`fc26b2e0d653df04b8d9877b09f87fddf76eddc3`. This records completed functional
tests, not a claim that expiring raw CI artifacts remain available indefinitely.
Later documentation-only corrections do not change that tested implementation.

- Complete Linux unit discovery: 750 cases, 5 explicit environment skips;
  actual MySQL tests ran separately.
- Real Nginx and Direct/Relay ACME/TLS issuance, renewal and restart checks passed.
- Three-node Chromium acceptance: 19 checks passed, including Local/Direct/Relay
  encrypted media and recording lifecycle, strictly serial STUN, and serial
  single-core CPU recovery checks.
- Actual MySQL migrations: 4 cases passed. Cold SQLite/MySQL database and control
  restoration: 2 cases passed, checking all 39 SQLite / 38 MySQL fixture tables.
- Actual identity-preserving endpoint handovers: 2 cases passed.
- Dev [CI #885](https://github.com/wongyiuming/FrontierCloud/actions/runs/38263696018)
  and PR [CI #886](https://github.com/wongyiuming/FrontierCloud/actions/runs/38263703959)
  passed source and required complete native acceptance. Their workflow records
  identify the tested commit; attached detailed artifacts expire after 14 days.
- Automatic review completed for that source without new findings; all previous
  review findings were corrected and verified before promotion in PR #73.

## Development evidence collected before exact final acceptance

These are component results while integration is still in progress; they do not
replace an exact-source final Compose acceptance:

- Original Python baseline `60f2260`: 547 discovered unit cases, 546 passed and one
  external MySQL case skipped; disposable temporary storage exposed sufficient
  filesystem space for the original 1 GiB reserve invariant.
- SQLite/Python database changes: 38 passing source/runtime cases plus one legacy
  external-MySQL skip; a separate real MySQL 8.4.11 fixture executed four migration
  and preservation cases successfully.
- Browser encryption/recording components: 50 Linux single-CPU cases; an actual
  Uvicorn socket with Chromium WebCrypto proved key unwrapping, two-chunk Range
  decryption, tag rejection and revocation. The production process never decrypts
  ordinary media content.
- Python certificate components: 33 cases, including a real local ACME protocol
  server and real TLS hot-loading. Actual Nginx issuance/renewal/recreation and
  Direct/Relay Compose issuance, HTTPS, pairing, STUN and restart checks passed.
- Integrated full source/unit snapshot: 629 discovered cases passed, with five
  explicit external-MySQL skips; the separate real MySQL fixture covers four of
  them. Final exact-commit acceptance remains pending while integration changes
  are still being applied.
- Operator cold recovery: actual SQLite and MySQL projects restored into new
  Compose projects. SQLite included 39 tables (including sqlite_sequence/BLOBs),
  MySQL 38 application tables, with typed hashes/rows matching; Redis AOF and
  secrets/ACME/premaster recovery matched and media/sockets stayed excluded.
- Admin navigation and fullscreen progress regressions were reproduced before
  the change and corrected in real Chromium in both repositories. Gin fixes are
  independently released in PRs #12 and #13; the Python archival release remains
  unpromoted until all final gates pass.

The acceptance record states unsupported features explicitly.
DNS-01 and direct Gin/Python database interchange are not implemented. Public
DNS/HTTP-01 reachability remains a deployment prerequisite; the Python native
fixture uses a real local ACME test CA, with test-only trust roots.
