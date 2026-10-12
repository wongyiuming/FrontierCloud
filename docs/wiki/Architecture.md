# Architecture

[ARCHITECTURE.md](../../ARCHITECTURE.md) is the authoritative architecture source;
this page is a navigation aid rather than a duplicate specification.

One single-worker Python/FastAPI business Master owns catalog, lyrics, users,
playback facts, encryption key grants and audit state. Followers provide assigned
Storage/Backup resources through verified, authenticated node communication.
A media object is complete on exactly one physical owner. There is no alternate
business authority or Compute Worker.

SQLite is the default durable database; the original optional MySQL is retained.
Redis provides bounded cache/security state. The browser owns ordinary media
AES-GCM encryption/decryption, continuous playback and capture computation.
The backend owns permission, metadata, integrity, key wrapping and consistency.

Business uses Python ACME plus Nginx HTTPS on 443. Direct Storage terminates
native Python HTTPS on 443. Relay exposes only its authenticated private
transport. Storage HTTP-01 temporarily occupies 80 and releases it afterward.
Accounts, private keys and verified complete generations persist across updates.
No product container controls host systemd or Docker.

Uploads explicitly select plaintext or ciphertext each time; timed recovery
requires writer-lease and physical-state proof. Priority dragging includes all
siblings and commits a minimal collision window atomically. Same-priority order
is deterministic. Every storage STUN endpoint is probed serially.

See [automatic TLS](../automatic-tls-python.md),
[browser encryption](../browser-media-encryption.md),
[media ownership](Media-and-Storage.md),
[release migrations](Release-and-Database-Migrations.md) and
[engineering gates](Engineering-and-CI.md).
