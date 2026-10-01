# Shared database schema

Schema generation 2 has one logical model and two reviewed physical snapshots:

- `mysql/0002-schema.json`: existing InnoDB DDL, including binary media paths,
  generated active-ban uniqueness, indexes, and preference constraints.
- `sqlite/0002-schema.json`: corresponding SQLite DDL and indexes. Audit keys
  use `INTEGER PRIMARY KEY`, dates use UTC ISO text, and backup chunks use BLOB.

Each JSON array entry is one SQL statement. There is no runtime SQL translation.
Python reads these files; Go embeds these exact files into its binary. A schema
change must update both backends and advance the same migration generation.
Business identifiers and node protocol values never depend on physical row IDs.

Generation 1 -> 2 introduces the migration journal. Its name and checksum retain
the existing MySQL migration identity, including when SQLite applies the equivalent
physical operation. SQLite runs initialization and upgrade inside `BEGIN IMMEDIATE`;
MySQL uses a connection-scoped migration lock, with resumable DDL and an atomic
generation/journal update. Both reject future generations, incomplete schemas,
and nonempty databases without a generation marker.

Changing `DB_TYPE` selects a database; it does not copy or migrate business data.
SQLite databases belong to a single host and must not be shared over NFS.

## Rollout status

Go supports independent initialization (`frontiercloud migrate`), database health,
and the protocol cryptographic primitives. Python supports both database connection
policies, schema initialization, and the media-object repository. Other Python
repositories and Go business handlers are still being ported. The existing Compose
deployment therefore continues to use Python/MySQL until business parity is tested.

Go foundation check:

```sh
docker build -f Dockerfile.gin -t frontiercloud-gin .
docker run --rm -e DB_TYPE=sqlite -v frontiercloud_sqlite:/data frontiercloud-gin migrate
```

Target deployment defaults remain Gin + SQLite. Runtime selection, the optional
MySQL Compose service, Go updater, and mixed Direct/Relay topology tests are pending.
