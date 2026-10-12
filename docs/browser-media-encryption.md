# Browser media encryption

FrontierCloud keeps media encryption and decryption in the browser. The Python
business node authorizes access, derives small file keys, and wraps those keys;
Local, Direct and Relay storage paths transport the same opaque ciphertext.
Storage Followers do not initialize a media premaster and cannot issue file keys.

Every file or folder selection must explicitly choose `encrypted` or `plain`.
The choice is discarded after that selection. Missing or inconsistent mode,
descriptor, preparation token or ciphertext length rejects the upload. An
encryption failure never switches the upload to plaintext.

## Wire format

Version 1 uses AES-256-GCM in independent 1 MiB plaintext chunks, each with a
16-byte authentication tag. An 8-byte random nonce prefix and a big-endian
32-bit chunk index form the nonce. Additional authenticated data binds the file
ID, original length and chunk index. Range playback needs only the chunks that
cover the requested plaintext range. The browser authenticates every chunk before
returning plaintext to its media element or download stream.

The descriptor contains `version`, `algorithm`, `file_id`, `nonce_prefix`,
`plaintext_size`, `chunk_size` and `ciphertext_size`. It contains no key. File
keys are HMAC-SHA256 derivations from an independently persisted 32-byte
premaster and the random file ID. Standard primitives come from `cryptography`;
the Python service does not encrypt or decrypt media content.

Browser handshakes use ephemeral P-256 ECDH, HKDF-SHA256 and AES-GCM envelopes
for the 32-byte file keys. This format matches the compiled browser encryption
and service worker assets. Asset obfuscation is an additional distribution
measure and does not replace standard cryptographic security.

## Authorization lifetime

A handshake is consumed once when creating a browser authorization. Its fixed
15-minute deadline covers multiple file requests within the same browser binding.
File-key requests do not extend that deadline. Expiry, explicit revocation or
process restart requires a new handshake. Admin key requests additionally
revalidate the current authenticated Admin session; public key requests recheck
current node role, source existence, visibility and storage placement.

The registry allows at most 3584 public and 512 Admin authorizations, and at most
16 authorizations per binding. Public handshake creation has separate fixed
one-minute limits of 64 per verified client IP and 8 per cookie. These bounds do
not charge media Range requests or evict another live grant.

An authorization cannot prevent a browser from retaining a delivered key or
plaintext. Revocation stops future key delivery. It is not digital rights
management and does not erase information already received by a client.

## Durable state and recovery

The private file `data/media-keys/media-premaster.key` is stored with mode 0600
inside a 0700 directory. The database stores its domain-separated verifier in
`media_crypto_keys`; `media_encryption` stores descriptors by stable object ID.
The database never stores a media key. Missing, malformed or mismatched secrets
fail closed when encrypted state or a recorded key identity already exists.

Back up the database and this private directory independently. Restore them
together before startup. Ordinary Follower business backup packages contain
descriptor/key-verifier facts, not an unencrypted premaster. Losing the premaster
cannot be repaired by creating a new random key for existing ciphertext.

Rename retains the stable object ID and descriptor. Transaction rollback restores
the original bytes and metadata together. Final deletion retires the descriptor
but keeps the consumed file ID as a tombstone; a new object cannot reuse its
nonce identity. An explicit recovery of the same object must restore the same
descriptor, never assign that file ID to a replacement upload.

Mixed downloads use a browser archive plan. Plaintext entries in that plan are
also bound to a stable object identity and source revision so an old plan cannot
silently download a replacement file. An encrypted archive is limited to 5000
files; larger selections containing only plaintext keep the existing server ZIP
path.

## Recording lyric snapshots

An encrypted lyric source requires a browser-encrypted snapshot in the recording
footer. Preparations bind the authenticated owner, browser binding, signed source
handle and current lyric file ID. Hidden, removed or changed sources cannot
authorize a new snapshot. The ticket reserves a descriptor and metadata hash;
storage capabilities carry only those small facts, never snapshot ciphertext.
The storage node verifies the opaque footer against the signed reservation before
publication, and the Master checks the physical receipt again before finalizing.

Saved snapshots belong to the authenticated recording owner. They can still be
read after the source track is hidden or removed. Python returns a wrapped key
and opaque snapshot; the browser decrypts it. Cancellation and deletion retain
the consumed file-ID tombstone. Kernel-owned recording leases prevent stale
cleanup from deleting or refunding a recording with a live writer.

## Validation

`tests/test_media_crypto.py` covers primitive/wire boundaries, fixed expiry,
admission, preparation integrity and secret restoration.
`tests/test_media_crypto_http.py` uses real SQLite transactions and FastAPI
requests for current permission checks, opaque Range/HEAD and download identity.
`tests/test_recording_crypto.py` checks signed snapshot capabilities and the
Local recording lifecycle. `tests/test_encrypted_media_lifecycle.py` loads the
actual application API router graph and tests managed upload/rename/delete and
rollback with the real schema and filesystem.

`tests/media_crypto_browser.py` exercises the compiled browser protocol in real
Chromium over a real Uvicorn socket, including ECDH/HKDF, multi-chunk Range
decryption, corrupted authentication tags and revocation. This protocol test does
not replace complete Compose, Direct/Relay or browser upload/playback acceptance.

`tests/federation_stack.py` and `tests/federation_crypto_acceptance.py` build the
actual Python Business/Nginx, native Direct Storage and native Relay Storage
images. A disposable local ACME CA issues certificates through real HTTP-01;
test-only child images and a private Chromium NSS profile trust that test root.
The clean runtime images contain no test CA, custom CA configuration or TLS
verification bypass. The test host needs Docker, Playwright Chromium, `certutil`
and the locked Python dependencies used by the driver.

The fleet runs actual AdminGUI per-selection encryption uploads, opaque disk
checks, compiled Service Worker cross-chunk Range/HEAD and decrypted downloads,
public encrypted playback/fullscreen seeks, encrypted lyrics and microphone
recording snapshots, remote rename/hide/restore/delete, priority pointer and
keyboard changes, certificate-pair download buttons and serial STUN probes.
After browser work closes, each one-core Web must recover below 20 percent CPU
for five consecutive samples. Fixture containers, volumes, networks, private CA
keys and browser profiles are removed on completion or failure.
