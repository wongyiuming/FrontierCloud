# Releases and database migrations

The online upgrade/rollback API, Admin release module and privileged Updater are
retired. Software replacement is an explicit operator maintenance-window action.
External CD belongs to its operator; this product does not install host services.

The repository still requires exact `dev` CI, complete native acceptance, one
same-repository `dev -> main` PR and identical reviewed/main code trees. Publishing
source, deploying software and completing a database migration are separate facts.

## Persistent recovery boundary

Back up every application database table before migration, together with node
identity/vault, runtime secrets, media premaster identity/key and the entire
private ACME account plus published certificate generations. The ordinary
Follower business Backup is a bounded subset and deliberately excludes the
Master premaster; it does not replace a full operator backup.

Stop all writers before taking a cold recovery snapshot, including Web and the
certificate agent. SQLite uses a consistent Connection.backup copy plus a full
SQL dump and actual SQL restoration/table hash verification. MySQL deployments
retain complete cold-volume recovery with the exact recorded image and platform.
Protect snapshots with private permissions and copy verified artifacts to an
independent machine. Media backup is an independent operator decision.

Never use `docker compose down -v` for upgrades. Never delete ACME volumes, node
vault, premaster or runtime secrets as cache cleanup. Unix sockets and temporary
Web/cache files are regenerated rather than restored.

## Database generations

Default SQLite is a repository-owned schema contract. Optional original MySQL
remains supported. Existing marked generation 1/2 databases migrate in order:

- generation 3 widens priorities to signed integers in -7..500000 without
  resetting historical values or unrelated business rows;
- generation 4 adds browser encryption descriptors/key identity and the signed
  encrypted-storage upload marker without decrypting media in the backend.

Unknown future schemas, unmarked initialized databases and mismatched migration
history fail closed. Retry an interrupted migration only with the preserved
recovery artifacts and the same reviewed release. A software rollback never
implicitly reverses the database. Restore the full compatible database and
control state if a migration must be reversed.

Equal generation numbers in the Python and Gin repositories do not make their
SQL schemas or node vaults interchangeable. Cross-language conversion requires
its own audited converter and is outside normal restore.

## Operator procedure

Follow [the recovery and unarchive guide](../operator-recovery.md) for the actual
backup/restore commands, deployment image selection, maintenance fence, readiness
checks and rollback boundary. Existing external certificates require the fixed
offline import described in [automatic TLS](../automatic-tls-python.md), before
removing retired certificate path variables. New installations request their own
certificates automatically.

Do not reopen business traffic until schema validation, authenticated node
health, trusted HTTPS served-certificate proof and representative media Range
requests pass. Remove only the operator's own maintenance fence after acceptance;
Admin cannot override it.
