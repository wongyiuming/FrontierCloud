# Native observation contracts

The Admin nodes observation API reads fresh identity, storage membership and
heartbeat facts from SQL. Desired settings are not evidence of effective remote
settings. An absent observation is awaiting; mismatched settings are syncing;
offline relationships are offline. Only the exact current membership relationship
can supply remote facts. Reads do not initiate network probes or expose sealed
credentials/public signing keys. Non-Master roles do not expose a stale Master
pool. Backup health retains disabled/running/failed/first/stale/healthy precedence.

The storage display exposes physical total/free, current allocation and project
usage alongside existing reservation/availability fields. The synthetic Auto
entry does not inflate aggregate capacity. Display calculations grant no write
reservation: the SQL placement transaction retains its own physical/logical
ceiling and native uploads supply current disk capacity.

Playback continuity diagnostics preserve the deliberately temporary reference
contract: 24 reports, five-minute TTL, process memory only and mandatory retirement
at 2026-10-15T00:00:00Z. They are not stored in SQL, Redis or files. Sanitization
limits strings, nesting, dictionary slots and timeline length and strips sensitive
keys recursively. Follower reports use a 2.5-second bounded signed upstream relay;
failure falls back locally. Only an authenticated active downstream relationship
can relay into a Master. Admin snapshot/clear uses existing session/CSRF rules.
Restart destroys reports; they are not authoritative cross-worker business state.

`/metrics` preserves case-sensitive Bearer authentication against the current
metrics token file and hides missing/invalid credentials with 404. Rotation is
observed without restart. Native collectors expose real HTTP counts, histograms,
in-progress requests, recovered exceptions and readiness dependencies, plus Go/
process facts. Route templates and bounded method labels prevent arbitrary path,
query, identity or method cardinality. No database backend label changes the
public dependency contract.

`/docs`, `/redoc` and `/openapi.json` require the existing Admin session. The schema
is a reviewed language-neutral JSON artifact embedded in the binary; a development
oracle compares it to the effective reference schema. Runtime startup, request
handling and schema serving do not invoke Python. Interface coverage and behavior
acceptance remain independent of schema registration.
