# Native offline maintenance fence

This is infrastructure for safe recovery, not a restore command. It fences
native Go processes sharing one **local** `DATA_ROOT`. It does not prove that
legacy Python workers, remote nodes, direct SQL clients or other processes with
another data directory have stopped. Do not run it against a deployed mixed
runtime and interpret its success as permission to replace SQL or media.

## Operator commands

Use the same environment and volume mounts as the native application:

```sh
frontiercloud maintenance enter --wait-seconds 30
frontiercloud maintenance status --wait-seconds 30
frontiercloud maintenance resume --wait-seconds 30
```

The optional wait is 1–300 seconds. A successful command emits a JSON report
with `enabled`, `native_quiescent`, `scope`, logical database diagnostics and
`restore_ready=false`. No report exposes identity keys or business rows.
Diagnostics reject missing stores, unknown roles and unsupported generations;
they do not initialize a schema. SQLite inspection uses `mode=ro`, which cannot
create a missing database or admit SQL writes. MySQL diagnostics use a read-only
repeatable transaction; connection establishment also receives the wait context.

`status` with an open fence reports no quiescence or database snapshot. With a
closed fence it waits for exclusive native lifecycle ownership before inspecting
SQL. `resume` checks the existing store while stopped, durably removes the fence
and permits **normal native startup**. It does not restart a container itself.
It deliberately preserves pending durable intents so normal recovery can run;
`logical_idle=false` is not permission to discard an upload or reservation.

`native_quiescent` describes the check performed under an exclusive lease, not
a transferable authorization token. After `resume` returns, a runtime can
already be starting. Even after `enter` succeeds, a separate future recovery
operation must acquire and retain its own offline lease throughout publication.

## Drain and failure behavior

Two persistent OS lock files serialize operator control and cover the entire
native lifecycle. They must never be unlinked or replaced to break a live lease:
doing so can create two independent lock inodes. The runtime lease is shared by
native processes; offline checks require exclusive ownership. Local Windows and
Unix use the existing cross-process file-lease implementations; network-volume
lock semantics and other-host database ownership are not established here.

`enter` writes and syncs `.frontiercloud-native-maintenance`, including its
directory, **before** waiting for running native processes. The exact marker
content is `frontiercloud-native-maintenance-v1` followed by a newline. Root
operator-created private gate files inherit the local data-volume owner's UID
and GID so the normal application account can observe and lock them.

Native processes check this marker before initialization or schema mutation,
before each HTTP admission, and periodically during their lifecycle. A closed
or unsafe marker cancels the runtime context. HTTP requests, startup recovery,
security publication, global delete, recording and backup workers are drained;
handlers and background workers finish cleanup before service resources close
and the lifecycle lease is released. A handler that ignores cancellation keeps
the offline command waiting rather than producing a false successful proof.
Initializers and migrations acquire the same lifecycle lease.

Timeout, cancellation, inspection failure or process crash leaves the fence
closed. OS process death releases leases, but the marker persists across
restart. An invalid marker, symlink or unexpected lock type fails closed; the
command does not erase malformed state to repair it. If reopening cannot be
durably acknowledged, it attempts to reinstate the fence and reports failure.
An unsuccessful command does not emit a successful JSON proof. Do not remove
the marker automatically in a health check or restart loop.

When the Nginx data mount corresponds to this `DATA_ROOT`, its hard fence takes
precedence over the existing manual/release `force-open` and Admin, cluster
update, health and static exemptions. A fixed internal maintenance page remains
read-only and prevents recursive 503 handling. This Nginx rule still does **not**
stop legacy Python background workers. The Go HTTP layer also refuses traffic
that bypasses Nginx with 503, `Cache-Control: no-store` and `Retry-After: 5`.

## Remaining recovery gates

The SQL snapshot counts unresolved media mutation, upload, placement, recording,
account, reserved storage, cold transfer and worker states without changing them.
`rename_done` is reported separately as native completed history, not proof of
physical manifest validity. Old Python recovery cannot safely consume that native
rename history; validated history drainage is still required before rollback.

No current command establishes all-writer isolation, validates physical journals
or historical media ownership, remaps identities and relationships, publishes a
recovered database/filesystem atomically, or grants Master/Follower promotion.
The existing startup refusal for incomplete cluster roles remains unchanged.
Restore, legacy adoption, updater replacement and mixed-runtime acceptance are
separate unfinished gates.
