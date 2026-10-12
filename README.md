# FrontierCloud

FrontierCloud is a self-hosted media browsing, continuous-audio playback, karaoke, cluster-storage, backup, and administration system. FastAPI currently provides the business/control plane, native browser JavaScript provides the UI, and Docker Compose runs the service stack.

The core product model is deliberately small: one business Master owns business truth, Followers provide Storage and/or Backup resources, and every managed media object has one complete physical owner.

This is the recoverable Python/FastAPI edition. The final release preserves
runnable `main` and `dev`, source history and frozen offline images; see
[recovery and unarchival](docs/operator-recovery.md). The active Gin implementation
is [FrontierCloud-Gin](https://github.com/wongyiuming/FrontierCloud-Gin).

## Current capabilities

- Music and video catalogs backed by managed media storage.
- MP3 continuous playback through one browser `MediaSource` / `SourceBuffer` session.
- Bounded playback-clock buffering, silent interrupted-read recovery, and HTTP Range repositioning for seeks beyond the current buffer.
- Synchronized/fullscreen lyrics, explicit lyric relations, and non-destructive same-name auto-link.
- Karaoke entry from media playback, guest preview, account recordings, and storage quota.
- Master/Follower storage placement using Local, Direct, or Relay transport.
- Transactional upload, visibility, priority, same-parent folder rename, and recovery-aware deletion.
- Browser encryption, session-scoped key grants, and safe interrupted-upload recovery.
- Priority dragging with stable ties and the full -7..500000 range.
- Automatic ACME certificates, node health/control, Admin audit facts and bounded Backup artifacts.

## Quick start

HTTP Standalone mode needs no `.env` file:

```bash
docker compose up -d --build --wait
```

Open `http://localhost`. The startup initializer creates the managed media tree under `data/media` and the persistent runtime secrets required by the stack.

For HTTPS, point DNS at this node and create `.env`:

```dotenv
TLS_ENABLED=true
SERVER_NAME=4399.com
```

Run the same Compose command. Python automatically issues and renews certificates;
public HTTP/HTTPS use 80/443. No external certificates or certificate paths are
required. HTTP-01 needs publicly reachable TCP 80. Fixed Master/Follower roles
require verified HTTPS and fail closed while issuance is unavailable.

Native Storage uses [the storage deployment guide](docs/automatic-tls-python.md)
and `scripts/deploy-storage.sh`: Direct serves public 443; Relay uses private
authenticated node transport. Its certificate agent occupies 80 only during
HTTP-01 validation. Ordinary media encryption/decryption runs in the browser.

Detailed configuration, generated-secret recovery, first Admin access, role initialization, and persistent-volume guidance live in [Deployment and Configuration](docs/wiki/Deployment-and-Configuration.md).

## Documentation map

The three top-level documents have separate jobs:

- **README.md** — product entry point, capability summary, shortest startup path, and navigation.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — authority, storage, mutation, playback, security, and release invariants that implementation must preserve.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — repository topology, change discipline, required checks, and promotion procedure.

Operational and subsystem detail belongs in the project Wiki sources:

- [Wiki home](docs/wiki/Home.md)
- [Deployment and configuration](docs/wiki/Deployment-and-Configuration.md)
- [Operations and troubleshooting](docs/wiki/Operations-and-Troubleshooting.md)
- [Cluster and resource model](docs/wiki/Cluster-and-Resource-Model.md)
- [Media and storage](docs/wiki/Media-and-Storage.md)
- [Playback continuity](docs/wiki/Playback-Continuity.md)
- [Engineering and CI](docs/wiki/Engineering-and-CI.md)
- [Release and database migrations](docs/wiki/Release-and-Database-Migrations.md)
- [Command reference](docs/wiki/Command-Reference.md)
- [Audit and validation](docs/wiki/Audit-and-Validation.md)

The published GitHub Wiki is available at [github.com/wongyiuming/FrontierCloud/wiki](https://github.com/wongyiuming/FrontierCloud/wiki).

The [final Python acceptance ledger](docs/python-final-parity.md),
[automatic TLS guide](docs/automatic-tls-python.md),
[browser encryption contract](docs/browser-media-encryption.md), and
[operator recovery guide](docs/operator-recovery.md) describe the final release.
Online software upgrade/rollback APIs and their Admin module are removed.
Use the documented maintenance-window procedure for installation and migration.

## Repository delivery

FrontierCloud has exactly two canonical branches: `dev` for implementation and
complete CI, and `main` for reviewed release history. Do not create additional
branches. The only valid promotion PR is same-repository `dev -> main`; after
merge, fast-forward `dev` to the resulting `main` commit before the next change.
The final release preserves the Python/FastAPI runtime and frozen recovery
artifacts; [unarchive instructions](docs/operator-recovery.md) keep it usable.

Read [ARCHITECTURE.md](ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md) before changing cross-cutting behavior.
