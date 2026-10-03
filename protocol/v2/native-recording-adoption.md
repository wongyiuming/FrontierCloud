# Native historical recording adoption

Python Follower recording files can predate a local SQL publication ledger.
Native deletion must not infer ownership or refund quota from filenames alone.
The offline commands preserve the existing node identity, active upstream,
recording/user IDs, file bytes and already charged capacity.

Stop all legacy and remote database/file writers in addition to closing the
native maintenance fence on both nodes. The local fence only protects native
processes sharing DATA_ROOT, not Python processes, other volumes or remote SQL.
Run `recording-inventory --relationship <id> --confirm-node-id <master-id>` on
the existing Master and securely save stdout as an inventory file. No new node
key is generated. The current Master signs its complete ready-recording set for
that active downstream relationship using its existing Ed25519 identity.
The inventory is valid for 30 minutes and binds schema generation 2, Master,
Follower, relationship, user/recording IDs, filename, type, exact size and hash.
It contains no password, relationship credential, signing key or account data.

On the existing Follower, first run `adopt-storage` for any legacy audio/video,
then `adopt-recordings --relationship <id> --confirm-node-id <follower-id>
--file <inventory-file>`. The input is limited to 4 MiB and 5000 recordings;
ambiguous JSON, unknown fields, missing fields and duplicate IDs are refused.
The native recording mutation lease covers the complete bounded scan and SQL
transaction. Every expected file must have the exact size and SHA-256. Unknown
files, other relationship directories, partial uploads, recovery journals,
symlinks and missing/mutated files are refused and left untouched.

The transaction rechecks the current Follower, schema, upstream and pinned
Master key. It requires no pending business work/reservations and exact equality
between existing used capacity and proven media-receipt plus recording bytes.
Unexplained capacity, foreign local rows, deleted owner/recording tombstones,
omitted live recordings and conflicting native receipts prevent publication.
New ready receipts and their audit records commit atomically. There is no quota
charge/refund, account insertion, credential reset or ID recreation. Retries
revalidate the same physical inventory and return zero newly adopted receipts.
Existing matching native recording rows and their metadata are retained.

This is ownership-ledger adoption, not portable business restore or permission
to repurpose cold backup relationships. It does not reopen maintenance or lift
the production cluster-startup acceptance gate. The inventory is sensitive
operational data; transfer and retain it privately and do not publish stdout.
