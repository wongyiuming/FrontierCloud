# Go-only runtime and validation

## Supported combinations

Only Gin/Go serves the backend and updater. The default is Gin + SQLite; the
only database alternative is Gin + MySQL. .env selects DB_TYPE and the MySQL
Compose overlay; it does not migrate existing data. Python deployment is
prohibited, its Docker/Compose entrypoints are removed, and Python executable
entrypoints fail explicitly.

app/, main.py and updater/server.py remain non-executable syntax references.
Their execution is not supported or tested. Reference maintenance may document
the same algorithms without promising a runnable Python product. Production
build context excludes application Python, tests, scripts and their dependencies.
Tests/scripts may use Python to drive real Gin or inspect native contracts.

Preserving existing serialized keys, tokens, password hashes, media IDs,
backup data and node relationships is mandatory. Go legacy-data vectors are
not Python application interoperability acceptance.

## Replacement coverage

Retired Python/FastAPI component tests and mixed-runtime drivers are replaced
by native Go package coverage, Python-driven Gin HTTP and the five-node matrix:

| Retired target area | Native behavior coverage |
| --- | --- |
| Database/schema/transactions/audit | internal/store/business, internal/store/sqlite, migrations |
| Admin key/auth/session/CSRF/security | internal/admin, internal/security, internal/httpapi |
| Media upload/delete/rename/visibility/priority | internal/media, internal/store/business, internal/httpapi |
| Lyrics/manual/auto link/default fallback | internal/media/lyrics*, internal/httpapi/lyrics_admin* |
| Catalog/playback/Range/download/brand | internal/media, internal/brand, internal/httpapi |
| Karaoke users/quota/recording lifecycle | internal/recording, internal/store/business, internal/httpapi |
| Pair/role/heartbeat/resource transport | internal/node, five-node native matrix |
| Backup/protocol/token/capabilities | internal/backup, internal/protocol, protocol/v2/vectors |
| Release/updater/handoff/rollback/cleanup | internal/release, internal/updater, native release matrix |
| HTTP black-box in Python | tests/test_gin_http.py, both SQLite/MySQL real native processes |
| Browser/UI/continuous audio | retained Python UI contracts, JS smoke, isolated real Chromium |

The HTTP driver starts its own native binary, fresh temporary data/secrets and
loopback listener. It accepts no production target URL. scripts/test-native-api.sh
owns disposable Redis/MySQL containers and runs it for both database choices.
Full Go tests include transaction rollback, media mutation races, persisted
identity, security and transport failures rather than substituting source-string
assertions for runtime behavior.

## Development-host gates

```bash
bash scripts/test-native-api.sh
bash scripts/test-go-deployment.sh
bash scripts/test-go-business.sh
bash scripts/test-native-default.sh
bash scripts/test-go-updater.sh
bash scripts/test-native-matrix.sh
bash scripts/test-native-release.sh
```

Fleet acceptance uses exactly five native nodes: one Master, two Direct, two
Relay, repeated with SQLite and MySQL Master selection. Follower database choices
alternate, so both drivers participate in each fleet. Fixtures use private CA
HTTPS, exact private source artifacts, disposable namespaces and independent
data. They never reuse or stop a deployed node and never globally prune Docker.
The release matrix retains upgrade/rollback, updater handoff, business/data
transport, backup and restart assertions at the smaller fleet size.

Hosted CI only runs scripts/test-ci-source.sh and bounded metadata promotion
checks, with parallel jobs capped at three minutes and no serial dependency
chain. It does not build Docker, run a database/fleet/browser/race acceptance,
install FastAPI, or evade limits through background work.

## Documentation and release scope

Wiki guidance is maintained in the separate
[GitHub Wiki](https://github.com/wongyiuming/FrontierCloud/wiki); docs/wiki is
untracked/ignored. The prior reconstruction evidence is retained as an explicitly
[historical audit](audits/go-reconstruction-through-2026-10-05.md), not current
operator instructions.

No branch consolidation or production deployment is implied by this policy.
gin_dev-to-gin_main and dev-to-main remain authorized provenance pairs pending
a separate owner decision. A local/private test artifact is not published CI
proof or authorization to replace production.
