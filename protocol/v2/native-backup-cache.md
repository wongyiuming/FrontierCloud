# Native backup cache ownership and cleanup

Native export artifacts and preflight scratch work are disposable, not business
truth. Each new artifact/child has an exact native ownership marker and holds a
shared `.cache.lock` OS lease through its entire read/check/close lifecycle.
The retained lock inode is never deleted. Cleanup obtains an exclusive lease;
it times out rather than deleting another worker's active files. OS process
death releases the lease, allowing explicit cleanup of known native leftovers.

Only exactly named owner-marked artifact pairs and preflight children are
eligible. Scratch children may contain only the native marker and known regular
SQLite scratch files. Unknown bytes/names, legacy unmarked files, extra child
content, changed inodes and symlinks are preserved. Cleanup never targets the
caller root or authoritative database; it can make partial cache progress before
an error, but that is not a transactional business restore.

The offline `cleanup-backup-cache --confirm-node-id ID --wait-seconds N` command
requires a persistent closed native maintenance fence, the selected existing
store, compatible generation and a decryptable current identity matching `ID`.
An SQL intent audit must commit before deletion; a failed audit deletes nothing.
The exact targets are DATA_ROOT/.business-backups and DATA_ROOT/.backup-preflight.
No arbitrary destructive target argument exists. Unknown entries are counted,
not exposed as sensitive paths. A final audit is required before a successful
report, and maintenance remains closed with `restore_ready=false`.

The generic verifier can use an operator-provided scratch directory, but this
command does not clean an arbitrary external directory. Use the fixed native
preflight cache when its explicit maintenance cleanup is desired. Windows and
Linux subprocess-kill, active-reader, ownership, cancellation, symlink and audit
fault tests verify these boundaries.
