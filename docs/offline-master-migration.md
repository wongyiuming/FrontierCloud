# Offline legacy Master admission (same MySQL)

This is an operator-only first-stage migration, **not** ordinary release
promotion, a DB_TYPE switch, a new Master identity, or automatic failover. A
normal native startup still rejects an unproven legacy Master/Follower.
Deployment requires the owner's explicit migration authorization and successful
tests of the exact immutable native source. Canonical branches are not merged
or rewritten by this workflow.

## Preconditions and recovery evidence

1. Preserve original MySQL, Redis, runtime secrets, media/brand/configuration,
   TLS material, original source SHA and images, Compose settings, and volumes.
   Make independent consistent SQL, RDB and file backups; verify their hashes.
2. Restore those artifacts into **new empty private storage** with the same
   MySQL version and database name. Use an egress-blocked Docker network: a
   restored Master must never contact production Followers. Retain untouched
   backups independently of the writable rehearsal.
3. Test recovery and the native runtime on a separate rehearsal copy. Before
   production cutover, stop legacy Web/Updater and every filesystem writer,
   close public traffic with a maintenance page, and take final SQL/RDB/config
   backups. Refresh the restored inventory from this final snapshot. Read-only
   hashing may be expensive; do not run it in normal request handling.

`master-migration snapshot` writes a private inventory from the restored store:

```sh
frontiercloud master-migration snapshot \
  --node-id EXISTING_MASTER_ID \
  --mysql-socket /var/run/mysqld/mysqld.sock \
  --manifest /proof/restored.json
```

Select `DB_TYPE=mysql`, the **original** database, existing secrets and data root.
The manifest must be outside the inventoried roots. Root credentials are read
only from the existing `mysql_root_password` secret file; they are never printed
or put on the command line. The socket and ordinary runtime connection must
resolve to the same server UUID. The command acquires and holds MySQL's global
read lock, verifies/decrypts existing identity and relationship credentials,
checks pending business intents, and hashes **all** tables, full table DDL,
all rows, data files and secret files. NULL and binary values remain distinct.

The local native maintenance fence remains closed. It is **not** proof that
legacy/remote filesystem writers have stopped; the operator must prove that
separately. Symlinks, special files, pending physical journals, unsupported
schema generation, custom routines/events/triggers, active unfinished work,
and inconsistent reserved-byte accounting fail closed. Missing unreferenced
historical lyric files are not invented or deleted. Only expired unstarted
reservations on revoked remote relationships may carry as unchanged history.

## Admission and cutover

Mount the independently restored inventory read-only on the stopped original
Master and pin the SHA256 printed by the snapshot command:

```sh
frontiercloud master-migration admit \
  --node-id EXISTING_MASTER_ID \
  --mysql-socket /var/run/mysqld/mysqld.sock \
  --manifest /proof/restored.json --proof-sha256 RESTORED_MANIFEST_SHA256
```

Admission re-inventories the original store under the same exclusive local
maintenance lease and authoritative database read lock. Any mismatch rejects
publication. A successful comparison binds the unchanged authoritative store
and durably publishes the vault-sealed native receipt for the **same** Master ID.
The database fence remains held until receipt publication is durable. No
business row, Admin Key, relationship, media file or expired intent is repaired,
cancelled or discarded to make the check pass. Existing malformed or foreign
receipts cannot be overwritten.

Explicitly resume native maintenance, start the tested candidate behind public
maintenance, and verify sessions, catalog, priorities, lyrics, recordings,
security, WebRTC, node relationships and real remote streaming before reopening
traffic. Keep the original MySQL/Redis containers and original data mounts;
never run `compose down -v`, prune volumes, or initialize replacement secrets.
Automatic upgrading must not promote an unreviewed runtime profile.

## Rollback

Before reopening public traffic, stop candidate writers and restore the old
Web/Updater/Nginx image/configuration against the retained original volumes.
Only restore the final frozen database if rehearsal/startup changed business
state and no new public writes were accepted. After traffic is reopened, an old
snapshot is **not** a lossless rollback: preserve all newly accepted writes and
validate reverse compatibility first. Keep backups and the signed runtime
provenance; do not reset identity or re-pair nodes as a rollback shortcut.
