# FrontierCloud Protocol v2

Status: compatibility baseline for the existing Python runtime.

This document extracts the node-to-node contract from the Python runtime. It is
the interoperability boundary for every FrontierCloud runtime and database
backend. A runtime may use any internal architecture, but messages on this
boundary MUST have the same bytes and semantics.

The normative fixtures in `vectors/` are part of this specification. Where this
document and an implementation disagree, a protocol change must update this
document, the shared vectors, and compatibility tests together.

## 1. Scope and compatibility

Protocol v2 covers node identity, pairing, confirmation, relationship
authentication, heartbeat, revocation, media transfer, storage control,
recording control, backup control, and cluster control under `/internal/v1/`.
The HTTP path remains `/internal/v1/` for compatibility; the signed payloads
carry `protocol: 2` or `v: 2`.

Runtime language and database backend are private node details. They MUST NOT
change this contract and MUST NOT be sent as protocol fields. In particular,
database keys, SQL dialects, connection data, transaction state, and filesystem
database paths never cross this boundary.

Unknown JSON object fields SHOULD be ignored unless accepting them would weaken
authentication or authorization. Required fields and their existing meanings
MUST NOT change within protocol v2.

## 2. Transport

- Node control traffic MUST use HTTPS with normal certificate verification.
- Node origins are HTTPS roots without user info, query, or fragment. The
  canonical origin omits port 443 and a trailing slash.
- Clients MUST NOT follow redirects for control requests.
- Control requests and responses are JSON objects. An empty authenticated GET
  has an empty body; a JSON request body is canonical JSON.
- Ordinary control bodies and responses are limited to 512 KiB. The existing
  recording-stat response exception is limited to 5 MiB.
- The path used by authentication is the exact path plus the exact query string,
  for example `/internal/v1/heartbeat?probe=2`.
- The current success contract is HTTP 200 with a JSON object. Non-200 responses
  are failures; callers MUST NOT depend on localized error text.

## 3. Primitive encodings

### 3.1 Identifiers

- A node, relationship, nonce, request, trace, or recording identifier is 32
  lowercase hexadecimal characters unless an endpoint says otherwise.
- A media or object identifier is 64 lowercase hexadecimal characters.
- Node relationship credentials decode to exactly 48 bytes.

### 3.2 Base64URL

Binary protocol values use RFC 4648 Base64URL without `=` padding. Decoders add
padding only for decoding and reject characters outside `A-Z`, `a-z`, `0-9`,
`_`, and `-`.

### 3.3 Canonical JSON

Canonical JSON bytes are produced as follows:

1. Encode as UTF-8.
2. Sort every object by key in ascending Unicode order.
3. Emit no insignificant whitespace.
4. Emit Unicode characters directly rather than ASCII or HTML escaping them,
   except for escaping required by JSON itself.
5. Reject NaN and positive or negative infinity.
6. Preserve the distinction between JSON integers and binary64 floating-point
   values: `1` encodes as `1`, while `1.0` encodes as `1.0`. Finite floats use
   their shortest round-trippable decimal representation, fixed notation for
   decimal exponents -4 through 15, otherwise scientific notation with a sign
   and at least two exponent digits. Negative floating-point zero is `-0.0`.
   Wire decoders must preserve integer precision and numeric type.

Signatures and token MACs operate on these exact bytes, not on a re-serialized
equivalent object. See `vectors/canonical-json.json`.

## 4. Ed25519 identity envelopes

A node private key is 32 raw Ed25519 bytes encoded with unpadded Base64URL. Its
public key uses the same representation. A signed envelope is:

```json
{"payload": {"...": "..."}, "signature": "base64url-ed25519-signature"}
```

The signature input is `canonical_json(payload)`. Verifiers MUST validate the
signature before trusting or interpreting the payload.

`GET /internal/v1/identity?challenge=<32-lower-hex>` returns a signed identity
payload containing:

- `node_id`, `role`, `endpoint`, and `public_key`
- the exact caller-provided `challenge`
- `protocol` equal to 2
- `app_version`

Responses use `Cache-Control: no-store`. Roles are `Standalone`, `Master`, and
`Follower`. A non-Standalone identity endpoint must match the HTTPS origin used
to reach it.

## 5. Pairing lifecycle

Only a Follower issues a one-time pairing package. The package is an Ed25519
envelope whose payload contains `node_id`, `endpoint`, `public_key`, `token`,
`nonce`, `expires_at`, and `protocol`. It expires after 300 seconds.

The Master imports that package, verifies the Follower identity over HTTPS,
creates a 32-hex relationship id and a 48-byte credential, then sends:

`POST /internal/v1/pair`

```json
{
  "package": {"payload": {}, "signature": "..."},
  "relationship_id": "...",
  "credential": "...",
  "master": {"payload": {}, "signature": "..."}
}
```

The Follower verifies the package, its one-time state, and the Master's signed
identity before creating a pending upstream relationship. A successful response
is `{"state":"pending","protocol":2}`.

The Master authenticates `POST /internal/v1/confirm` using the pending
relationship. The Follower activates it and returns
`{"state":"active","protocol":2}`. Pairing packages cannot be reused, and a
Follower has at most one non-revoked upstream Master.

`POST /internal/v1/revoke` is authenticated and idempotently records peer
acknowledgement for an already-revoked relationship. It returns
`{"state":"revoked","protocol":2}`. A revoked credential cannot authenticate
ordinary control calls.

## 6. Relationship authentication

Every protected control request carries:

- `X-Node-Relationship`: 32-lower-hex relationship id
- `X-Node-Time`: Unix time in decimal seconds
- `X-Node-Nonce`: 32-lower-hex fresh nonce
- `X-Node-Signature`: lowercase hexadecimal HMAC-SHA256

The MAC key is the decoded 48-byte relationship credential. The signing input
is UTF-8 bytes of six lines with no trailing newline:

```text
relationship_id
timestamp
nonce
UPPERCASE_HTTP_METHOD
exact_path_and_query
lowercase_hex_sha256_of_body
```

Receivers accept timestamps at most 60 seconds from their current time and MUST
atomically reject a nonce previously accepted for that relationship. Method,
query, and body bytes are therefore authenticated. See `vectors/node-auth.json`.

## 7. Heartbeat

`POST /internal/v1/heartbeat` is relationship-authenticated. A Master may send
the Follower its selected transfer `mode` (`Relay` or `Direct`) and resource
configuration. A Follower rejects configuration arriving in the wrong
relationship direction. Every response contains `app_version` and `protocol`;
a Follower also returns its resource and storage-capacity summary.

An incoming heartbeat does not by itself mark a peer reachable. Reachability is
established by that node's own successful outbound probe. The nominal interval
is 30 seconds and the offline threshold is 120 seconds.

## 8. Capability tokens

Media, storage, and recording capabilities use:

```text
base64url(canonical_json(payload)) + "." +
base64url(HMAC-SHA256(credential, encoded_payload_ascii))
```

They expire 300 seconds after issue. Verifiers require `v == 2`, `e > now`, and
reject expiries more than 60 seconds beyond the normal lifetime. They also bind
the relationship, Master, owning node/user, object, and operation as shown by
the fixtures:

- `vectors/media-token.json`
- `vectors/storage-token.json`
- `vectors/recording-token.json`

Tokens are bearer capabilities and MUST NOT be logged. Media tokens may include
signed `request_id` and `trace_id` provenance. Storage tokens bind `upload` or
`delete`, path, and size. Recording tokens bind `upload`, `stream`, or
`download`, size, content type, and filename.

## 9. Existing protected endpoint families

The current v2 compatibility surface includes:

- `/internal/v1/backup/*` for begin, chunk, commit, and abort
- `/internal/v1/storage/*` for upload, stat, and delete
- `/internal/v1/media/*` for Direct and Relay media access
- `/internal/v1/recordings/*` for upload, stream, stat, and delete
- `/internal/v1/media-control/*` for directory mutation
- `/internal/v1/cluster-update/*` for update control and status
- `/internal/v1/playback-diagnostics`

Endpoint-specific authorization always includes relationship direction, role,
and resource ownership checks in addition to a valid MAC or capability token.

The existing cold backup transfer messages and limits are described in
[`backup-transfer.md`](backup-transfer.md). This does not define a new backup
artifact format or permit online reads from a recovery artifact.

## 10. Conformance and evolution

All maintained runtimes MUST consume the shared vectors directly in tests.
Changes to canonical bytes, signed fields, headers, expiry rules, status codes,
or endpoint paths are protocol changes, even if decoded JSON looks equivalent.

Capabilities negotiation, a database-independent backup format, and a
runtime-independent release manifest are required evolution work but are not
silently invented by this baseline. They must be added compatibly, documented,
and covered by cross-runtime vectors before use.

