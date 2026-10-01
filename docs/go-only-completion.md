# Go-only backend completion gate

The target is a standalone Go backend, not a Python wrapper. The historical
Python source remains a contract oracle until parity is established; it must
not appear in the final runtime, initializer, migration, maintenance or updater.
The default is Gin + SQLite. DB_TYPE=mysql selects the same logical schema on
MySQL. Existing frontend, Nginx, Redis and coturn remain unchanged.

`protocol/v2/http-routes.json` records the effective routes (including integrity
overrides) from the reference application, rather than all decorated functions.
Each method/path must have a real implementation and behavior tests. A registered
route alone, an empty response, or a generic success response is not parity.

## Acceptance checklist

- [x] Exact canonical JSON, Ed25519, HMAC and capability conformance vectors
- [x] Independent Go image, initial secrets/media and schema migration commands
- [x] Shared logical schema, SQLite and MySQL initialization and migration tests
- [ ] Complete environment configuration and selected database deployment
- [ ] Public pages, catalog, streaming, playback, lyrics and brand
- [ ] Admin authentication, CSRF, keys, tree, upload, visibility and deletion
- [ ] Directory rename/priorities, lyric management and security observability
- [ ] Node identity, fixed role, pair/confirm/revoke/re-pair and heartbeat
- [ ] Master global catalog, storage allocations and Direct/Relay capabilities
- [ ] Recording accounts, quotas, upload/finalize/download/delete
- [ ] Logical business backups, promotion and restore with either database
- [ ] Go updater, release verification, maintenance, rollback and cleanup
- [ ] Only-Go deployment contains no Python process or Python-dependent script
- [ ] Isolated 1 Master / 3 Direct / 6 Relay private-CA integration validation

Do not switch the existing development topology to Go until the replacement
passes its integration gates. Do not claim completion from schema tests alone.

## Current verified slice (2026-10-01)

The Go runtime currently supports Standalone public media pages/catalog,
local streaming with Nginx acceleration (or Go Range serving), playback
accounting, lyrics, opaque karaoke handles, brand assets, Admin login/status/
logout, temporary keys, key rotation, and the Admin shell. Environment loading
supports `.env` with explicit process-environment precedence.

The native Admin media implementation now includes managed hierarchy browsing,
visibility, directory/media priority, phrase-aware Chinese/pinyin search,
streaming media/lyric upload, single-file and streaming ZIP downloads, and
journaled deletion. Upload/delete intents survive uncertain commits and restart;
an unresolved intent blocks media traffic/readiness instead of claiming success.
The embedded pronunciation data is immutable; Python is not invoked for search.
Directory rename preserves stable media identities, play statistics, visibility
and lyric links through a durable replayable intent. Explicit and automatic lyric
relations are transactional, including reverse edits and concurrent matching.
Cross-process volume leases prevent worker races; killed processes release their
locks and new workers preserve live multipart uploads.

IP bans, whitelist, attack accounting and policy generations are authoritative
database state. A Go publisher produces the existing Nginx TSV snapshot; Redis
outage cannot bypass a ban. Native network observations preserve verified client
identity, have Redis cooldowns and transactional grouped summaries.

Real disposable MySQL and Redis checks pass, including concurrent stable-ID
registration, exact-once playback accounting, temporary-key redemption,
session TTL, rotation, CSRF and Admin HTTP contracts. The full Go race suite
passes on Linux. Windows Go tests pass as well. The latest real MySQL/Redis run
also verifies security policy concurrency, network observations and HTTP parity.

The latest disposable Linux test run also verifies concurrent/idempotent upload
publication on real MySQL, positive multipart/CSRF upload contracts on real
Redis, exact-case metadata deletion, ZIP content and recovery fault injection.
Remote source hashes were checked against the local files. The Python reference
suite passes 567 tests (4 skipped), including shared search vectors.

The next verified domain slice includes TLS-verified, pinned Ed25519 node
challenges, canonical control HMACs, bounded non-redirecting HTTPS transport,
one-use pairing packages, encrypted relationships, durable replay rejection,
confirmation/revocation/re-pair, and outbound-only heartbeat reachability.
Master promotion/adoption is atomic with storage allocation and audit, retaining
local media IDs. Follower configuration preserves reservations and backup state;
the retired remote Compute Worker remains disabled.

Real MySQL checks also pass for site-type upload reservations, same-folder
placement affinity, category layout fences, physical/logical capacity limits,
and idempotent global publication. Unknown expired physical placements retain
their reservations until confirmed cleanup.

Native HTTP pair/confirm/revoke/heartbeat handlers now use bounded raw control
bodies and exact-path HMACs. The global public catalog, virtual category/player
pages, exact-once playback, default/explicit lyrics, Direct capabilities and
Nginx Relay destinations are implemented. Follower media serves verified
capabilities with strict paired-origin CORS, Range and HEAD, never a public
independent business catalog. Master Admin virtual tree/search, visibility,
priorities and lyric management retain global IDs without manufacturing local
identities for remote tracks.

Follower uploads reserve quota before streaming, use bounded buffers, persist
publication intents and exact object IDs, and atomically commit accounting with
audit. Lost-response retries do not replace or re-charge files. Startup recovers
completed intents and abandoned reservations while preserving another worker's
live OS lease. Real MySQL verifies concurrent owned upload reservations and
idempotent publication; SQLite fault tests cover interrupted/lost commits.
The HTTP pair → heartbeat/configuration → signed upload → authenticated stat →
global finalize → Direct Range/HEAD chain passes; Relay remains Nginx delegated.
This is not yet a complete Admin upload/delete or cluster deployment workflow.

This is NOT a complete backend replacement. Existing Master/Follower identities
are explicitly refused at startup until the cluster implementation is complete.
The default deployment and updater still contain Python and must be replaced
only after the remaining acceptance gates pass. The original test topology has
not been redeployed.
