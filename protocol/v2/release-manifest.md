# Runtime-neutral release manifest v1

The optional `release-manifest-v1` feature retains protocol v2. A common
`release_version` labels one release and a canonical SHA-256 identifies its
entire manifest. Neither an application version, language nor DB_TYPE determines
protocol compatibility; `protocol` and `schema_generation` remain explicit.

The complete bounded manifest has `main` and `gin_main` publication profiles,
each mapping to an immutable Git-archive artifact with its own production
commit, reviewed source commit and tree SHA. Profiles represent the existing
fixed local publication policies (`main/dev` and `gin_main/gin_dev`). The Master
must send the whole manifest, never choose a Follower's implementation or copy
its own target SHA to every peer. Each peer selects only its locally configured
policy. SQLite/MySQL does not enter artifact selection or wire identity.

Manifests are at most 8192 bytes. Unknown fields/profiles, duplicate JSON fields,
trailing input, malformed SHAs, boolean/floating protocol numbers, missing
artifacts and unsupported protocol/schema are rejected. See the schema and
shared Python/Go vectors for the exact wire format and canonical digest.

Parsing, a Master signature or a manifest digest alone is NOT publication proof.
Each selected artifact requires independent exact production/source/tree
evidence, an exact merged same-repository source PR and the newest corresponding
successful source push CI. An older success cannot supersede a newer failure.
Rollback additionally requires historical artifact proof and production ancestry;
it must not pick an unrelated local prior SHA or manufacture a previous manifest.

Both runtimes now implement the parser/resolver, independently verified local
artifact queue, durable joint history, authenticated whole-manifest dispatch and
bounded common-digest/private-artifact convergence. Updater status RPC advertises
the optional feature only when its agent supports it; it remains absent from the
general node identity/heartbeat baseline during integration acceptance.

Master Admin requests remain empty: a caller cannot select SHAs or send a
manifest. Set `RELEASE_MANIFEST_PATH` to an absolute container path and explicitly
mount publication metadata read-only there. Both production artifacts require
independent exact CI/PR/tree proof and must be current publication HEADs for an
upgrade. Rollback uses only the durable previous whole manifest and independently
verifies both historical artifacts; each updater also enforces local production
ancestry. This optional file is metadata, not proof or a signature bypass.

Without that setting, legacy same-SHA releases remain fail-closed across different
publication branches. Publication-asset automation and real mixed-runtime fleet
upgrade/restart acceptance are still pending. Parser/unit/RPC tests do not claim
those acceptance gates have completed.
