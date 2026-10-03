# Native deployment candidate

`docker-compose.gin.yaml` is an isolated candidate, not permission to redeploy an
existing cluster. Its default database is SQLite and it has no MySQL service,
volume or dependency. `docker-compose.gin-mysql.yaml` adds the optional MySQL
service and explicit readiness dependency. The unchanged root Compose file is
the legacy deployment until final integration acceptance.

In `.env`, select `COMPOSE_FILE=docker-compose.gin.yaml` for Gin/SQLite, or
`COMPOSE_FILE=docker-compose.gin.yaml:docker-compose.gin-mysql.yaml` for
Gin/MySQL (use `;` as the separator on Windows). An external MySQL deployment
can select `DB_TYPE=mysql` and a stable `MYSQL_HOST` without the service overlay.
Use a distinct `COMPOSE_PROJECT_NAME`, `DATA_DIRECTORY`, database and secrets
volume for acceptance fixtures. The required `FRONTIERCLOUD_REVISION` is a full
source commit SHA, not a branch, runtime compatibility version or schema version.

The fixed Web commands, initializers and updater use native binaries. The Web
process is UID/GID 10001, has no Docker socket, drops capabilities and has a
read-only root filesystem. The separately restricted updater uses only the
local Engine API. Its project selector must match the Compose project. Redis,
Nginx and coturn retain their established protocol roles.

## Database selection is durable configuration

Before the first native database writer, the data root stores a fsynced
`.native-store` binding under a retained cross-process lease inode. The binding
fingerprints SQLite's absolute path or MySQL's stable host/port/database, never a
password, credential or user. Credential rotation therefore does not switch the
store. A subsequent different selection fails before opening/initializing an
empty database. If an older vault exists without a binding, native admission
must first decrypt the identity in the selected *existing* store. Missing or
unrelated identity, unknown binding bytes and symlinks fail closed.

Changing `.env` is not a database migration. Host aliases and SQLite paths must
remain stable; intentional data migration requires separately verified offline
transfer. A failed first connection may leave the intended binding; it must not
silently select an unrelated store on the next attempt.

## Evidence and remaining boundary

`scripts/test-go-deployment.sh` parses actual Docker Compose JSON and checks
commands, permissions, source labels, optional database dependency and project
selection. `scripts/test-go-updater.sh` runs private SQLite and MySQL stacks,
including native initialization, actual builds, self-handoff and rollback.
Both database variants have passed, including checks that no Python interpreter
exists in the Web/updater and MySQL does not create an authoritative SQLite DB.

Compose build labels by themselves do not prove that a mutable build context
matches the supplied SHA. Reviewed production artifacts require immutable Git
archive builds and exact CI evidence; the updater already uses that boundary.
Final default promotion, portable restore, mixed releases and cluster startup/
restart acceptance remain gates. Do not run this candidate against existing
ten-node volumes to bypass them.

## Native role restart provenance

After every service and media/recording recovery has initialized, native startup
publishes a fsynced private `.native-runtime` receipt under a retained local
lease. The vault-sealed receipt binds the current node ID and `.native-store`
fingerprint. A later native Master/Follower may restart only with matching
provenance, and still runs all recovery before accepting traffic. Missing,
foreign, malformed and non-regular receipts fail closed for cluster roles.
No environment switch disables this check. A confirmed empty identity reset
returns to Standalone and may republish its new identity after complete startup.

Historical Python cluster identities without native provenance remain fenced;
storage/recording adoption alone does not issue a startup admission receipt.
Offline migration admission and actual ten-node restart are separate acceptance
gates, not implied by the receipt unit tests.

Nginx workers retain their own UID and use the fixed native media group 10001.
Private recording directories are group-traversable; only hash/size-verified
published native recordings become mode 0640. Secrets, operation intents,
leases and incomplete upload stages remain private. Recording byte locations
are internal and require the established account/capability authorization.
