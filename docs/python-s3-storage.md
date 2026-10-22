# Optional S3 storage: implementation contract

This document defines the optional S3 adapter's scope and acceptance contract.
The Python renewal and original Direct/Relay migration scope remain required.
The owner authorizes publication after positive automatic review, without a
separate S3 experience gate. Complete native acceptance and the other renewal
release gates remain mandatory before the repository is archived again.

## Product boundary

Nodes may register a private general-purpose AWS S3 bucket or a tested compatible
HTTPS endpoint as an optional storage member. No account purchase, bucket creation,
public bucket, bucket deletion, Docker control or bundled object store is part of
the product. The acceptance host uses an isolated, self-hosted SeaweedFS service;
that service is not an AWS S3 instance. Default Compose remains independent of it.

The first surface supports managed music/video objects. S3 is a distinct adapter,
not a Follower with invented heartbeat, filesystem free space or certificate-agent
status. Administrators set a logical quota. Unknown physical capacity and live
external bandwidth remain unavailable; they are never represented as zero load.
S3 does not participate in first-version karaoke, Follower business backups,
cross-member migration, safe Follower retirement or heavy-job traffic scheduling.
Existing Direct/Relay migration remains within the original renewal scope.

## Data and security

The Master remains catalog and authorization authority. Browser-to-S3 upload,
playback and download use expiring SigV4 grants; ordinary media bytes never relay
through Master. Browser encryption/decryption remains unchanged. S3 holds either
the explicitly selected plaintext or the original browser-produced ciphertext.
AWS server-side encryption is not a substitute for that boundary.

Credentials are entered through authenticated HTTPS Admin GUI and encrypted in
the Master vault. Configuration/status responses, logs and test receipts never
return the stored credential fields. The secret signing key never leaves Master.
A presigned URL necessarily carries its access-key identifier and, for temporary
credentials, the `X-Amz-Security-Token` value. It is a short-lived browser grant,
not a credential configuration response. Reports must redact complete presigned
URLs, including that token. Temporary credentials must expire grants
no later than the credentials themselves. TLS verification is mandatory. The
Master control client rejects redirects. Browser bytes are sent to the endpoint
selected by its configured member; an administrator must trust that provider
with the selected plaintext or ciphertext. Endpoint validation applies before
probing an administrator-supplied endpoint. A configured compatible endpoint may
be on a private network; this is not a public anonymous URL-fetch API.

Only the configured bucket/prefix is managed. Opaque immutable object keys are
separate from logical paths; rename must not rely on atomic multi-object S3 moves.
One logical object and its complete parent-folder ownership retain one member.
Preexisting bucket contents are not silently imported, renamed or deleted.
Disabling a member blocks new placement while existing reads retain explicit
authorization. Removal requires reconciled reservations and no owned objects.

## Upload and deletion lifecycle

Reserve quota and logical ownership durably before issuing grants. Use multipart
upload for large media, bounded part concurrency and bounded browser memory.
Record upload IDs and parts durably; an ETag is an opaque entity validator, not
a SHA-256 digest. Verify size, expected upload metadata and the selected supported
checksum contract before publishing a catalog row. Completion's HTTP 200 alone
does not prove success: its XML may contain an error. Ambiguous completion after
a timeout requires reconciliation before retry, abort or capacity release.

Use conditional publication and immutable session-owned keys to prevent an old
grant from overwriting an active object. Authorization/session expiry, browser
refresh, cancellation, failed parts and Master restart must leave a recoverable
upload record. Abort may race an in-flight part: reconcile with ListParts and
repeat abort when necessary. Never free a reservation based solely on a browser
disconnect, nor delete a published object to hide an incomplete upload.

A complete known create rejection with consistent bucket/object identity, or
local credential expiry proven before HTTP transmission, may return an unchanged
creation journal to its original reservation. No provider ID, grant, checksum or
output may already exist. The original deadline, quota and encryption descriptor
remain until ordinary retry, cancellation or expiry. Unknown outcomes, malformed
or conflicting responses and server failures still require provider reconciliation;
an empty upload list alone does not establish absence after a lost create reply.

Cancelled S3 upload and shared journals retain their `aborted` audit state.
Offline upgrade inspection admits that history only with matching local-Master
session, member, media, path and size, a released logical locator, settled grants,
no object ownership and no live encryption descriptor. Existing reservation gates
remain mandatory. Part audit rows and a normally removed member may remain;
an unrelated new media ID at the same path does not revive the old upload.
Unknown or inconsistent records still block upgrading. Inspection changes no rows.

S3 cannot report an application's active HTTP writer with native filesystem
locks. Define a durable session deadline and part-grant expiry; reconcile only
after that window, with restart-safe abort and retained error state. A provider
lifecycle rule for incomplete multipart uploads is a secondary operator safeguard,
not the application's only cleanup mechanism. Explain versioned-bucket retention
and object-lock restrictions instead of claiming DeleteObject frees physical bytes.

## Acceptance

The self-hosted fixture has a pinned image digest, separate data directory,
private credentials, no published internal management ports and confined TLS CA.
AWS SDKs may be used as independent acceptance clients; they are not implicitly
new product runtime dependencies. Record actual sanitized inputs and outputs.

Required cases include signed direct PUT/multipart, playback Range, original
filename download, CORS preflight and exposed validators, real certificate/SAN
verification, anonymous denial, bad signature, expiry, revoked credentials,
quota/collision/parent-affinity concurrency, checksum/length failures, ambiguous
completion, part/abort races, restart recovery and idempotent managed deletion.
Run actual browser encrypted and plaintext workflows; prove the Master does not
receive payloads and keys are not disclosed. Source-compatible behavior and local
tests do not establish actual AWS bucket/IAM acceptance.

The earlier fresh repository-native milestone passed 31 provider checks, four
provider-restart checks, 27 transaction checks plus four separate-process checks,
and 27 ordinary-trust browser checks. The browser covers plaintext/encrypted WAV
upload, exact original-name/content downloads, playback/seek, actual partial
transfer failures, refresh, GUI cancellation, live-grant deletion deferral,
Web restart, normal background recovery after real deadlines and final GUI
profile removal. The one-CPU / 512 MiB Web returned to five consecutive CPU
samples between 0.56 and 0.87 percent. Source and actual input records are in
the parity ledger. That snapshot did not cover unattended 30-minute idle expiry,
repeated long playback after multiple grants, the complete video/cache matrix,
combined real MySQL/browser/provider, owner acceptance or a real AWS bucket.

### Playback extension, 2026-10-20

A separate SQLite/verified-HTTPS Chromium snapshot passed 25 checks in 61.726
seconds. GUI uploads include a 135,290,000-byte encrypted MP3 and its next track,
plus plaintext and encrypted H.264/AAC MP4s. Actual MSE startup, manual next,
seeking to the real audio tail and automatic transition passed; the fixture does
not claim hours of elapsed playback. The observed S3 Range responses declared
3,267,304 bytes in total Content-Length for those music operations. This is not
measured network traffic; cancelled responses may deliver fewer bytes.
Video checks require decoded audio/video counters, the original dimensions,
non-muted state and an advancing clock after seek; they are not human listening.

A page reload reused the worker's warm ciphertext without a further S3 GET and
required fresh key authorization. This is bounded Service Worker memory, not
persistent browser storage or proof of reuse after browser restart. The Web ran
with one CPU and 512 MiB; final CPU samples were 0.63, 0.82, 0.83, 0.67 and 0.75
percent. Actual inputs, media hashes, source snapshots and sanitized results are
in private `s3-playback-nodes-progress-evidence.json`.

A subsequent combined real-MySQL/provider/browser run passed 43 checks in
1,897.639 seconds. It repeats upload, original-name downloads, playback and the
large/audio/video extension, then real partial-transfer failure, page refresh,
GUI cancellation and Web restart. A separate unfinished upload receives no
manual cancellation: its unchanged thirty-minute idle deadline expires and the
normal worker aborts its provider upload and settles quota. GUI pending rows
disappear, and disabling/removing the profile succeeds only afterward. No SQL
deadline, system clock or task state was rewritten. Five final Web CPU samples
were 0.58, 0.63, 0.65, 0.69 and 0.66 percent with one CPU and 512 MiB. This does
not establish multi-grant uninterrupted playback, a full fault/cache matrix,
owner acceptance or a real AWS bucket/IAM deployment.

The native S3 runner enables the playback extension by default. The complete
native gate also sets `FC_S3_LONG_IDLE=1` to repeat real idle recovery. Standalone
runs can opt into that wait with the same variable; `FC_S3_BROWSER_DATABASE=mysql`
selects the actual historical MySQL Compose overlay. The ordinary recovery worker
handles the wait without altered clocks or SQL deadlines; its standalone browser
timeout is extended accordingly.

The later current-source MySQL/provider/browser component passes 49 checks in
1,897.813 seconds, including compact Nodes UI, plain/encrypted uploads, original
downloads, large MP3 manual/real-tail transitions and actual thirty-minute idle
recovery. Its final Web CPU samples are 0.86, 1.24, 1.21, 1.08 and 0.75 percent.
Only its two exact disposable projects were removed after ownership-label checks;
bind-mounted diagnostic data and the owner's separate preview remain intact.
This is a private overlay component, not final-source release or owner acceptance.

The runner now also requires a read URL to expire using the actual clock. It
checks refusal of that original URL, obtains a new grant through the unchanged
application, compares an uncached browser-decrypted range, seeks in the long MP3
and switches at its real tail. Signed URLs stay private; receipts retain hashes,
deadlines, ranges and observed byte counts. The V8/V11 components pass this real
expiry gate within their recorded source scopes. Final-source acceptance, the
remaining full cache/fault matrix and owner acceptance are still required.

## Primary references

- [AWS managed S3 service](https://aws.amazon.com/s3/)
- [AWS presigned uploads](https://docs.aws.amazon.com/AmazonS3/latest/userguide/PresignedUrlUploadObject.html)
- [AWS multipart limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)
- [AWS multipart completion and embedded errors](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html)
- [AWS object version identity and deletion](https://docs.aws.amazon.com/AmazonS3/latest/userguide/versioning-workflows.html)
- [AWS abort and concurrent-part behavior](https://docs.aws.amazon.com/AmazonS3/latest/API/API_AbortMultipartUpload.html)
- [AWS browser CORS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/cors.html)
- [SeaweedFS API compatibility and differences](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)
- [SeaweedFS 4.48 multipart implementation](https://github.com/seaweedfs/seaweedfs/blob/4.48/weed/s3api/filer_multipart.go)

## Infrastructure progress, 2026-10-19

The isolated development fixture uses SeaweedFS 4.48 at pinned digest
`sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d`,
limited to one CPU and 512 MiB. Its Docker network is internal and publishes no
host ports. Credentials and TLS keys remain outside the repository. The service
is stopped after checks, with Compose and fixture data retained for integration.

The independent AWS SDK/client run passed 21 checks: private signed upload/read,
Range 206 and bytes, conditional overwrite protection, configured CORS and exposed
Range validators, hostile-origin denial, wrong CA/SAN rejection, invalid/expired
signatures, opaque ciphertext preservation with test-client decryption, two real
multipart parts and verified assembly, abort and non-publication of incomplete
objects, and cleanup of only the test-owned objects. A second process after
service restart passed persistence verification and repeated the three relevant
CORS/certificate checks. TLS verification was never disabled. The SDK is confined
to the independent acceptance image, not added to the product dependencies.

Initial failed runs exposed bind-mount credential permissions and the test
certificate profile required by the strict client. Those runs and actual
sanitized invocations are retained. Final source hashes, inputs and receipts are
in the private `s3-progress-evidence.json` and adjacent artifacts under the same
local evidence directory recorded in `python-current-parity.md`.

Those initial infrastructure results do not prove FrontierCloud node registration, durable S3 upload
recovery, browser playback/encryption integration, actual AWS IAM behavior or
owner GUI acceptance. Later service/browser milestones below have their own
source receipts; none establishes actual AWS IAM or owner acceptance.

The candidate `s3_signing` primitive signs only bounded metadata and short browser
object grants. It introduces no AWS SDK runtime dependency or media I/O. Eleven
unit cases include the AWS published header/query signature vectors, regional
virtual/path addressing, opaque prefix keys, URI encoding, credential/grant
expiry, forbidden authority/header injection and size/type limits. Credential and
request representations omit their fields. These are protocol primitives, not a
completed S3 storage adapter or authorization/lifecycle implementation.

## Metadata control and durable configuration candidate

The metadata-only `s3_control` client implements verified-TLS bucket HEAD,
multipart create/list/complete/abort, object HEAD and deletion. Responses are
bounded to 256 KiB with UTF-8/XML depth, node, entity, identity and field checks.
It distinguishes credential, access, signature, region, TLS, timeout, throttle,
checksum and conflict errors without reflecting arbitrary provider messages.
An embedded error in HTTP 200 is a failure. Unacknowledged or invalid mutations
retain uncertainty rather than implying reservation release.

Each part uses SHA-256; completion validates the expected composite SHA-256 and
object metadata/size can be checked independently with HEAD. ETag remains opaque.
SeaweedFS 4.48's ListParts response omits per-part checksum fields; these optional
fields may be absent, but completion still requires the independently recorded
part checksums and a matching final composite checksum. The actual pinned service
refused a bad part checksum and passed the project client's verified multipart
assembly, HEAD, deletion and abort checks. Thirty-one initial checks plus four
restart checks passed. These extend the earlier 26-check result; neither run is
an actual AWS bucket or FrontierCloud browser workflow.

Generation 6 adds four additive configuration/upload/part/object-binding tables.
`s3_members` uses the existing encrypted node vault, revision-bound probe results,
logical quota and serialized namespace admission. Configuration starts disabled
and unchecked. Status has no credentials and reports unavailable physical/live
traffic measurements as unknown. Overlapping prefixes and known AWS regional,
global or path/virtual endpoint aliases cannot register independent owners.
Unreconciled uploads retain quota and prevent credential/location replacement or
removal through ordinary configuration. A dedicated cleanup-credential replacement
requires an already disabled member, all pending uploads in `aborting`, and expiry
of every old grant. Namespace remains frozen and placement remains disabled; a new
probe is required before accepting uploads again. Owned objects freeze bucket/prefix/endpoint; replacing credentials then
requires another probe before accepting new work. Identity reset refuses to
orphan a configured member. Current registry tests do not prove the future probe
implementation, object admission, upload recovery or browser authorization.

The first S3 payload limit follows the existing media contract: 10 GiB including
browser encryption overhead. No video/audio transformation is introduced.

Complete generation-6 candidate discovery passed 908 cases (900 passed, eight
explicit database-fixture skips) plus all source checks. Seven actual MySQL
database cases passed, including failed DDL/retry, missing-index repair and old-row
preservation. Eight additional actual MySQL registry checks passed without bucket
I/O. Earlier SQLite success had not exposed MySQL's rejection of TEXT defaults;
the failed native run is retained and the affected IDs now use bounded strings.
These results are collected with actual inputs and exact overlay proofs in
`write-guards-s3-registry-progress-evidence.json`; they do not replace the remaining
durable upload, node UI, browser encryption/playback or owner acceptance gates.

A later repeat exposed restart readiness ordering in the pinned compatible
service: bucket metadata and TLS were available while object GET returned
`500 InternalError` until volume locations registered. A read-only diagnostic
then read the same retained object with its original SHA-256. The final repeat
passed 31 initial and four restart checks, recording one intermediate 500.
The restart fixture now waits at most 30 seconds for that exact committed object;
only known transient 500/503 codes are retried. Missing objects, denied access,
bad certificates and checksum differences remain immediate failures. It never
recreates the marker. This is startup readiness, not proof that ordinary failed
requests succeed, nor permission to discard unresolved upload state. The original
failed run and the diagnostic inputs remain alongside the successful repeat.

## Multipart service candidate

`s3_uploads` reserves both the S3 logical quota and the existing cluster path
reservation in one transaction before any provider call. It uses the current
media shared fence, placement lock and node-row SQL lock. Same-parent ownership,
category layout, duplicate paths, migration scopes and source drains are checked
without rebalancing around an unavailable owner. The original browser encryption
descriptor is stored once; no media key or payload is accepted by the service.

Parts are fixed at 8 MiB, with at most two unconfirmed live grants. Each grant
binds exact length and SHA-256, and its deadline is durable before it is returned.
Reissuing a part cannot change its checksum. Browser progress does not confirm
bytes; ListParts supplies size/ETag and optional returned checksum, followed by
mandatory composite SHA-256, size and session/object metadata verification at
completion. Publication and quota settlement share one SQL transaction. Lost
completion acknowledgements and post-provider SQL failures are reconciled from
the original immutable key, not by creating a replacement object.

The idle deadline is 30 minutes, renewed by authorized activity and confirmed
parts, with an absolute 24-hour session limit. Activity cannot extend a signed
part grant or invent confirmed progress. Cancellation first prevents new grants,
retains capacity through existing grant deadlines, aborts the provider upload and
checks its observable absence. A racing or ambiguous abort keeps its journal and
capacity. Unknown objects are not deletion targets. An uncertain create is found
only by the exact opaque key; an empty or ambiguous list cannot prove that a lost
create acknowledgement had no effect. Such a session remains explicitly unresolved.

`s3_recovery` is one asynchronous metadata worker, separate from heartbeats. It
processes at most four records per sweep, rotates failed records to avoid starving
later expiry cleanup, honors the operator hard fence and the existing kernel
upload claims, and joins on application shutdown. The current candidate's core
transactions have passed focused SQLite faults and an actual verified-TLS service
run with independent direct HTTP payloads and a separate recovery process. The
earlier service-only snapshot passed 928 complete unit cases (920 passed, eight explicit
database-fixture skips), source policies, 19 actual MySQL fault cases and 19 actual
S3 service checks plus four child-process checks. Product runtime hashes match
across the full/MySQL/native snapshots; later fixture cleanup checks and prose
have separate source records. See `s3-upload-progress-evidence.json` for actual
inputs and limits. These counts describe the earlier service milestone. Later
catalog, object and Admin edits have separate exact-source evidence in
[`python-current-parity.md`](python-current-parity.md). The later real-browser
milestone passes ordinary-trust plaintext/encrypted uploads, original-name/hash
downloads and public playback/seek. Full interruption/expiry coverage and owner
acceptance remain required.

## Admin controls and connection scope

The candidate node panel offers add/configure, test connection, enable/disable
and removal. An S3 upload site type uses the same music/video hierarchy, parent
ownership and browser encryption choice; it never selects an individual member.
Existing Direct/Relay uploads keep their original route. S3 profile requests are
bounded, authenticated HTTPS/CSRF controls; validation never echoes credentials
or preparation tokens. Configuration is revision checked. Pending transactions
remain visible with confirmed bytes, reconcile and cancellation actions.

Test connection performs a certificate-verified HeadBucket request. Success is
explicitly `bucket_connection`, not proof of CORS, PutObject, completion, reads,
deletion, KMS access or browser playback. The actual multipart transaction must
verify publication. Administrators retain responsibility for a private bucket,
the chosen region and scoped provider permissions. Changes of endpoint/bucket/
prefix are blocked while objects are owned; replacing credentials requires a
new connection check for further placement.

The native browser uploader holds one 8 MiB part at a time, hashes in Web Crypto,
and sends payload only in the S3 PUT. Metadata confirmation failures permit one
retry with the identical part checksum. Content-Length is supplied by the
browser from its Blob body, not set as a forbidden JavaScript header. Progress
remains below 100% until SQL publication succeeds. An inactivity deadline aborts
an unresponsive transfer; an authorized activity heartbeat renews only the
durable idle deadline, never a grant or confirmed byte count. Closing the page
does not release quota: the existing recovery worker reconciles after expiry.

For a browser at `https://media.example.com`, configure CORS on the bucket with
that exact origin. A console JSON rule for the current request headers is:

```json
[
  {
    "AllowedOrigins": ["https://media.example.com"],
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedHeaders": ["content-type", "range", "x-amz-checksum-sha256"],
    "ExposeHeaders": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
    "MaxAgeSeconds": 300
  }
]
```

The bucket remains private; CORS is browser permission, not object authorization.
`Content-Range` must be exposed, not merely present on the HTTP response. The
browser's encrypted-byte adapter checks its exact start, end and ciphertext
total before decrypting a chunk. A provider can return HTTP 206 while hiding
that header from JavaScript; the adapter then refuses the read. Enabling uploads
and passing HeadBucket therefore do not prove browser playback. Keep the range
check and AES-GCM authentication intact when correcting the bucket configuration.
See AWS's authoritative [CORS elements](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ManageCorsUsing.html).
For AWS general-purpose buckets, the connection test's HeadBucket permission
is the IAM action `s3:ListBucket`, not a separate `s3:HeadBucket` action. Its
generic 400/403/404 responses do not prove a particular credential or bucket
cause. See the authoritative [HeadBucket permissions and response limits](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html).
The principal also needs `s3:ListBucketMultipartUploads` at bucket level and
`s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListMultipartUploadParts`
and `s3:AbortMultipartUpload` for the managed key prefix. Versioned buckets also
need `s3:GetObjectVersion` and `s3:DeleteObjectVersion` for the exact version.
Provider KMS permissions remain a separate requirement, not established by a
successful HeadBucket probe. No bucket-creation, bucket-deletion, public ACL or
arbitrary object import is used. See AWS's [multipart permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html#mpuAndPermissions).

## Reproducible development acceptance

The candidate `scripts/test-python-s3.sh` joins the complete native runner after
the Compose fleet checks. It creates a fresh, private test directory, pinned
SeaweedFS image, internal network, temporary TLS chain and scoped test credentials.
No service port is published. The independent, pinned AWS SDK exists only in a
test-client image; credentials and server keys are outside its build context.
The ordinary browser trusts the temporary public roots in its own disposable
profile. The runner never installs a system root or ignores certificate errors.

The native gate exercises provider behavior and service transactions, then the
actual Compose/Admin browser lifecycle. Waiting for real grant expiry is part
of the gate: no clock or database deadline is rewritten. On failure it stops
Web before other fixture cleanup and retains data for diagnosis. Development-host
evidence is collected explicitly with `scripts/collect_native_evidence.py`;
private state and credentials are excluded. Hosted CI does not run this gate
or upload its evidence.
The S3 child allows 1,000 seconds normally and 2,850 seconds with actual idle
expiry. These bounded fixture timeouts include actual authorization waits; they
do not change any product deadline or resource limit.
This runner's fresh acceptance result is recorded separately from earlier
private-fixture milestones in the parity ledger.

The V8 MySQL/provider/browser candidate passes 59 checks in 760.051 seconds,
including 300.585 actual seconds of read-grant expiry. The old signed URL is
refused with 403; an uncached range is authorized again and decrypted in the
browser, and long MSE seeking and real-tail automatic next continue. GUI
deletion obtains fresh ordinary read grants before checking retained quota and
eventual settlement. This profile does not repeat V4's actual thirty-minute
idle-upload wait. Plain S3 selected/visible metadata predicates have a later
fix; V9 proves separate titles and original images but fails its range-budget
assertion. V10 records the real 206 ranges, exposing provisional request headers
which omit Range after redirect. V11 uses full request headers and observes both
the original Master Range input and direct S3 Content-Range/byte response. All
66 checks pass in 820.405 seconds, including distinct plain S3 selected/visible
titles and original picture hashes/dimensions. One ordinary small-track playback
GET is a complete response; it is not counted as an ID3 metadata read. The full
final-source gate remains required.
No self-hosted result is described as AWS real-bucket or owner acceptance.

## Published objects and cache

Catalog rows explicitly identify the S3 adapter; absence of a Follower relation
never selects a local-file fallback. Public and Admin authorization precede
short GET/HEAD grants. A permanent encryption flag is compared with the current
descriptor; missing or inconsistent descriptors fail closed. Original names are
used for downloads, and browser ZIP streams keep S3 data away from Master.
Plain S3 MP3 metadata uses the bounded browser Range/tag path; it never causes
Master-side media fetches. A batch containing S3 cannot silently fall back to
the server ZIP path when it exceeds the supported browser-entry count.

Publishing, pending deletion and final removal change a SQL catalog revision.
S3 cache namespaces include that revision, so lost Redis invalidation replies
cannot keep an older S3 publication namespace current. Namespace SQL failure
uses a live catalog instead of trusting an old cache. Logical folder rename
preserves the immutable S3 key, media ID, encryption descriptor and lyric links.

Deletion hides the catalog row, prevents new grants and retains quota through
issued grant deadlines. It verifies exact owned size, checksum, metadata,
ETag/version before deletion and confirms absence before settling quota and
business metadata in one SQL transaction. Lost provider/SQL acknowledgements
retain a retryable record. Expiry does not prove that a remote HTTP download
which began earlier has ended, nor erase a user's downloaded/cache copy; this
adapter does not advertise active-reader observation or Follower retirement.
Object Lock, denied deletion or unexpected object bytes remain an explicit
pending/error condition. Application quota is not a claim about the provider's
billable physical retention or other versions.
