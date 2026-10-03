# Native Go release agent

The Go business runtime never receives the Docker socket. The isolated
`frontiercloud-updater` binary exposes only a bounded local Unix control socket
with `status` and `start` requests. There is no TCP listener, command execution
API, Compose CLI, systemd or Python dependency. Control directory traversal and
socket access are limited to the fixed application group; status and private
replacement journals are separate, atomically synced 0600 files.

## Persistent release boundaries

A start is serialized with a one-task queue and published to disk before waking
the worker. Malformed/duplicate-key/trailing/oversized requests, unknown fields,
wrong types, non-full lowercase commit SHAs and unknown modes are rejected.
One OS runtime lease prevents a second agent from unlinking a live socket.
Corrupt persistent state fails closed; interrupted queues are not auto-replayed.

Production branches are explicitly `main` or `gin_main`. The separate native
Master verifier checks reviewed tree and newest successful source push CI before
queueing. The updater independently fetches the configured production ref,
requires exact HEAD for upgrade and production ancestry for rollback, and refuses
tracked working-tree changes. Immutable allowlisted `git archive` input excludes
working-tree data, secrets and `.git`; no checkout/reset is performed.

Images are pinned by full revision, native runtime/component and compatible
generation labels. Service discovery requires exactly one matching Compose
project/service. Snapshots retain mounts, environment, security, resource limits,
health configuration and explicit network aliases. Old container-ID aliases and
dynamically assigned addresses are not reused. An unrelated container occupying
a saved name is never removed. Container removal never removes business volumes.

Before replacement, a durable private journal preserves all original service
snapshots. A separate mounted native helper closes/drains the local lifecycle
fence; executing that command inside the draining Web container would kill the
helper prematurely. Offline preparation only admits an existing compatible
generation 2 store and never performs reverse DDL. Native initializers, Web
health and rendered/running Nginx configuration must succeed before committing
the new local generation. Docker stop/wait/build use different bounded deadlines.

Local replacement failure restores exact previous image IDs and configuration
under native maintenance. Uncertain or failed recovery retains its journal and
the release fence. Once the healthy local generation has been durably committed,
Follower distribution failure retains that generation rather than rolling it
back. The native Master convergence command uses fresh identity/relationship
checks, at most four calls concurrently, and bounded signed start/status polling.
Neither role changes nor peer database/runtime names are used as compatibility
shortcuts. A no-op generation retry still rechecks Web and Nginx health.

## Compiled updater handoff

Replacing services is not enough to update a compiled Go agent. A target-image
handoff helper receives its own durable journal, stops the old agent, replaces
only its exact saved container and starts the new immutable image. The restart
checkpoint is persisted before helper launch. Old-process cancellation cannot
overwrite it. The new agent checks its compiled revision, target service images,
health and persistent state before claiming success or reopening public traffic.
Mismatched/restarted old code fails closed. No mutable checkout is interpreted
as proof that the running binary changed.

Handoff helpers also hold an exclusive retained OS lease. A failed target create/
start restores the exact previous native updater image/configuration, publishes
a durable failed checkpoint and leaves the committed Web generation intact under
maintenance. A lost successful start reply is reconciled before fallback; a
running target is never replaced with old code merely because an acknowledgment
was lost. The replacement daemon proves its live control image before retiring
the exact project-owned helper and handoff journal. Unknown helper ownership or
fallback provenance is never treated as removal authority.

Only exact obsolete native release tags belonging to the configured project are
eligible for cleanup. Current/previous releases, unrelated tags/projects, shared
parents, business volumes and in-use images are not force-deleted. Helper
containers do not inherit service aliases or published ports.

## Site availability is not offline recovery

The existing Admin site maintenance API has a native implementation with
session/CSRF/verified HTTPS and an intent audit before mutation. Atomic public
flags and updater override clearing share an OS lease. Busy or unknown updater
state cannot authorize reopening. Explicit reopening after a failed release
retains the established manual override contract, but cannot defeat/remove the
stronger `.frontiercloud-native-maintenance` fence. Unknown flag bytes/types and
symlinks fail closed instead of being erased.

## Acceptance status

Concrete stateful Engine fault tests cover local replacement versus distribution
commit boundaries and immutable agent handoff. Linux real-driver/race checks and
real Docker archive/build/helper/container replacement tests have passed. The
full native disposable-stack upgrade/handoff/rollback test also passed, including
unprivileged Web access to the group-restricted socket. This uses a fresh private
Git origin and a separate Standalone project, not production CI or ten-node
mixed-runtime acceptance; do not interpret it as either of those gates.
Original development nodes remain untouched. Default deployment selection,
mixed-runtime release/recovery and cluster-role startup acceptance are separate
gates, not established by this document.
