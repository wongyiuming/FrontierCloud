# Go-only deployment

The root `Dockerfile` and `docker-compose.yaml` now select native Go. The retained
`Dockerfile.gin` / `docker-compose.gin.yaml` aliases are identical. The default
SQLite profile has no MySQL service, volume or dependency; the optional
`docker-compose.gin-mysql.yaml` overlay adds MySQL and its healthy dependency.
Python deployment is prohibited. app/ and Python entrypoint sources are non-executable syntax references only, excluded from native images. Database selection is not permission to overwrite an existing cluster's database, identity or physical ownership.

In `.env`, select the runtime/database combination:

| Runtime / database | `COMPOSE_FILE` |
| --- | --- |
| Go / SQLite (default) | `docker-compose.yaml` |
| Go / MySQL | `docker-compose.yaml:docker-compose.gin-mysql.yaml` |

Use `;` instead of `:` as the separator on Windows. An external MySQL deployment
can select `DB_TYPE=mysql` and a stable `MYSQL_HOST` without the service overlay.
Use a distinct `COMPOSE_PROJECT_NAME`, `DATA_DIRECTORY`, database and secrets
volume for acceptance fixtures. The required `FRONTIERCLOUD_REVISION` is a full
source commit SHA, not a branch, runtime compatibility version or schema version.

For fresh native bootstrap, export that exact SHA and run
`bash scripts/build-native-images.sh "$FRONTIERCLOUD_REVISION"`, then
`docker compose up -d --no-build --wait`. The builder sends only fixed paths from
`git archive` at the exact commit: never `.env`, data, keys, mutable edits or
untracked files. It builds images only and cannot replace services. Ordinary
Compose `--build` is useful for private testing, but its SHA label alone is not
source proof. Publication still requires reviewed source and exact successful CI.

The fixed Web commands, initializers and updater use native binaries. The Web
process is UID/GID 10001, has no Docker socket, drops capabilities and has a
read-only root filesystem. The separately restricted updater uses only the
local Engine API. It drops all capabilities except CHOWN for the private
control-socket group and DAC_OVERRIDE for selected-volume maintenance. Web
receives neither capability nor Docker authority. Its project selector must match the Compose project. Redis,
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
Development acceptance now uses five native nodes (one Master, two Direct, two Relay), repeated for Gin/SQLite and Gin/MySQL. It runs only on the development host, never hosted CI. Historical migration admission and physical ownership proof remain separate requirements; see docs/go-only-completion.md.

## Native role restart provenance

After every service and media/recording recovery has initialized, native startup
publishes a fsynced private `.native-runtime` receipt under a retained local
lease. The vault-sealed receipt binds the current node ID and `.native-store`
fingerprint. A later native Master/Follower may restart only with matching
provenance, and still runs all recovery before accepting traffic. Missing,
foreign, malformed and non-regular receipts fail closed for cluster roles.
No environment switch disables this check. A confirmed empty identity reset
returns to Standalone and may republish its new identity after complete startup.

Historical cluster identities without native provenance remain fenced;
storage/recording adoption alone does not issue a startup admission receipt.
Offline historical migration admission remains an explicit, separately verified operation. Removing Python runtime support does not remove historical data-format checks or permit bypassing provenance.

Nginx workers retain their own UID and use the fixed native media group 10001.
Private recording directories are group-traversable; only hash/size-verified
published native recordings become mode 0640. Secrets, operation intents,
leases and incomplete upload stages remain private. Recording byte locations
are internal and require the established account/capability authorization.

Native SQLite writers create/repair the exact selected database inode to mode
0600 before opening SQL. WAL/SHM inherit private database permissions. Read-only
backup inspection does not chmod or otherwise mutate an authoritative file.
