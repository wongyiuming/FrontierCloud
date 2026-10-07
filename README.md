# FrontierCloud

FrontierCloud is a self-hosted media browsing, continuous-audio playback, karaoke, cluster-storage, backup, and administration system. Go (Gin) provides the default business/control plane with SQLite; MySQL is the only database alternative. Python application sources are non-executable syntax references; Python remains available for test/script drivers only. Browser JavaScript, Nginx, Redis and coturn retain their existing roles.

The core product model is deliberately small: one business Master owns business truth, Followers provide Storage and/or Backup resources, and every managed media object has one complete physical owner.

## Current capabilities

- Music and video catalogs backed by managed media storage.
- MP3 continuous playback through one browser `MediaSource` / `SourceBuffer` session.
- Bounded playback-clock buffering, silent interrupted-read recovery, and HTTP Range repositioning for seeks beyond the current buffer.
- Synchronized/fullscreen lyrics, explicit lyric relations, and non-destructive same-name auto-link.
- Karaoke entry from media playback, guest preview, account recordings, and storage quota.
- Master/Follower storage placement using Local, Direct, or Relay transport.
- Transactional upload, visibility, priority, same-parent folder rename, and recovery-aware deletion.
- Asynchronous bounded Backup artifacts, node health/control, Admin audit facts, and reviewed cluster releases.

## Quick start

For a fresh HTTP Standalone node, build the exact committed native source. No
Python or MySQL service is required:

```bash
export FRONTIERCLOUD_REVISION="$(git rev-parse HEAD)"
bash scripts/build-native-images.sh "$FRONTIERCLOUD_REVISION"
docker compose up -d --no-build --wait
```

Open `http://localhost`. The startup initializer creates the managed media tree under `data/media` and the persistent runtime secrets required by the stack.

The two supported `.env` deployment selections are documented in
[Native deployment](protocol/v2/native-deployment.md). Changing the runtime or
database selection does not migrate existing state. Historical Python cluster
volumes remain fenced until a separately verified offline adoption/migration;
do not apply this quick start to an existing cluster.

Preserving an existing Follower's identity and media requires the separate
[verified offline Follower migration](docs/offline-follower-to-sqlite.md), not
reinitialization or re-pairing. Existing native MySQL Masters use the
[separate SQLite conversion](docs/offline-master-to-sqlite.md).

For HTTPS, copy the relevant switches from `.env.example`, enable TLS, set `SERVER_NAME`, and provide `certs/fullchain.pem` plus `certs/privkey.pem`. Fixed Master/Follower roles require certificate-verified HTTPS and fail closed when that contract is missing.

Detailed configuration, generated-secret recovery, first Admin access, role initialization, and persistent-volume guidance live in [Deployment and Configuration](https://github.com/wongyiuming/FrontierCloud/wiki/Deployment-and-Configuration).

## Documentation map

The three top-level documents have separate jobs:

- **README.md** — product entry point, capability summary, shortest startup path, and navigation.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — authority, storage, mutation, playback, security, and release invariants that implementation must preserve.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — repository topology, change discipline, required checks, and promotion procedure.

Operational and subsystem detail belongs in the separate GitHub Wiki (not tracked in this repository):

- [Wiki home](https://github.com/wongyiuming/FrontierCloud/wiki/Home)
- [Deployment and configuration](https://github.com/wongyiuming/FrontierCloud/wiki/Deployment-and-Configuration)
- [Operations and troubleshooting](https://github.com/wongyiuming/FrontierCloud/wiki/Operations-and-Troubleshooting)
- [Cluster and resource model](https://github.com/wongyiuming/FrontierCloud/wiki/Cluster-and-Resource-Model)
- [Media and storage](https://github.com/wongyiuming/FrontierCloud/wiki/Media-and-Storage)
- [Playback continuity](https://github.com/wongyiuming/FrontierCloud/wiki/Playback-Continuity)
- [Engineering and CI](https://github.com/wongyiuming/FrontierCloud/wiki/Engineering-and-CI)
- [Release and database migrations](https://github.com/wongyiuming/FrontierCloud/wiki/Release-and-Database-Migrations)
- [Command reference](https://github.com/wongyiuming/FrontierCloud/wiki/Command-Reference)
- [Audit and validation](https://github.com/wongyiuming/FrontierCloud/wiki/Audit-and-Validation)

The published GitHub Wiki is available at [github.com/wongyiuming/FrontierCloud/wiki](https://github.com/wongyiuming/FrontierCloud/wiki).

Development-host acceptance uses five Go nodes (one Master, two Direct, two Relay), repeated for SQLite and MySQL. Hosted CI stays within three minutes and does not run fleet, database or browser acceptance.

## Repository delivery

The authorized profiles are `gin_dev -> gin_main` for native Go and `dev -> main`
for canonical release history. Both permit only Go artifacts; branch consolidation is a separate owner decision. Do not create additional branches. Each promotion must
be same-repository, reviewed and backed by exact source CI; synchronize its
implementation branch after merge. Database selection never changes release profile.

Read [ARCHITECTURE.md](ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md) before changing cross-cutting behavior.
