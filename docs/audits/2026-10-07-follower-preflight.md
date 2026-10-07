# Remaining production Follower: pre-cutover evidence

This receipt records read-only production inspection and an isolated restored-data
rehearsal. It does NOT claim the production node was migrated or publicly reopened.
No production role reset, re-pair, media deletion or database correction has occurred.

The existing HTTPS Relay remains a schema-generation-2 Follower with one current
active upstream relationship and a preserved revoked historical relationship.
MySQL is 8.4.11. Its 679 registered videos are also registered as active on the
Master: 23,252,100,456 bytes. Three ready Master-owned recordings account for
4,211,100 further bytes. Their sum exactly equals the Follower's recorded usage,
23,256,311,556 bytes, within the original 25 GiB allocation.

The legacy Follower has 2,098,229,461 reserved bytes but no durable local upload
sessions. One 72,970,013-byte .cluster-upload partial has neither a Master
publication nor a Master upload intent. The Python reference upload implementation
persisted the counter but kept its refund flag in memory; process death could leave
this drift. Recovery is a separately fenced, audited accounting operation, NOT
permission for the new converter to silently refund or discard unfinished data.

Append-only private SQL/config/source/secrets and complete-media backups were
copied off-host. The 23,330,119,680-byte physical tar's SHA256 was checked at both
destinations. Restoring the tar on the development host and hashing every path
matched the live source baseline: 685 files, 23,329,281,635 bytes, including the
security projection, all videos/recordings/default lyric and original partial.
This live baseline still requires a FINAL frozen comparison before production
admission; hardlinks alone are never recovery proof.

The independent MySQL restoration reproduced the identity, relationships, media
count, quota, usage and reservation. A deliberate wrong expected counter rejected
the repair without committing. The correct fixture-only transaction modified
only the reservation and appended an audit. The original partial was retained
with its exact hash in a private quarantine.

Using the native offline operator on that fixture passed all 34 source table
cell digests, NULL/binary/JSON/timestamp/generated values and next-ID floors,
SQLite integrity, an independent SQLite recovery artifact, file/permission and
seven-secret proof. It admitted the ORIGINAL Follower identity under a closed
maintenance gate. Native storage adoption then added exactly 679 publication
receipts and corresponding audit events, without changing media IDs, allocation
or usage. All other table counts were unchanged, including eight security audit
rows, three security locks, four WebRTC events and one WebRTC summary.

Signed recording adoption, private native runtime/transport acceptance, FINAL
frozen source/independent-restoration equality and controlled public cutover
remain pending. The owner separately authorized a short Master maintenance window
to export its signed three-recording inventory with immediate resumption.

The resource-bounded native business/race and both five-node database matrices
have passed on the development host. Whole-release/default/updater gates were
still running when this receipt was written; no pass is claimed for them yet.
No publication or deployment authority is inferred from private synthetic CI.
