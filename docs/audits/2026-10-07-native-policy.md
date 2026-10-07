# Native-only policy verification receipt

The playback repair is a separate commit (6e0ccb5), not part of this runtime
retirement. Existing production deployments have NOT been changed by this
policy commit. Python reference source is retained; Python deployment and
application acceptance are removed. Serialized historical business data is
still supported by the native implementation.

Verified on October 7:

- All native Go package unit tests passed on the supplied development host.
- Fresh Gin processes passed six Python black-box HTTP tests for EACH of
  SQLite and MySQL: authenticated/CSRF mutation, AdminKey across restart,
  persisted priority, Range/path isolation, visibility/default lyric protection
  and brand upload/download/delete.
- Both actual native Compose database configurations passed deployment tests.
- The complete lightweight source gate passed locally in 4.411 seconds,
  including 77 Python source/UI/script tests (one native-process fixture class
  deliberately skipped without CLI fixtures), JS checks and all budget/policy
  guards. A prior development-host source gate took four seconds.
- Hosted CI remains capped at three minutes. Real stores, race, browser,
  five-node matrix and whole-release gates remain development-host only.
- The independent GitHub Wiki was updated at 0536a8f. Repository docs/wiki
  files are untracked/ignored, not deleted from the local working directory.

The real MySQL/Redis/race business gate started, but development-host SSH
subsequently stopped returning a protocol banner. No pass is claimed for that
unfinished gate or the subsequent five-node/whole-release gates. The race
runner now limits package parallelism and memory instead of assuming every
compiler can use the full shared host. Complete those gates after host access
recovers; do not use a lightweight green check as production migration proof.

The newly requested remaining production Follower migration is separately
scoped. Its MySQL identity, relationship, secrets and all media must be backed
up and independently restored before any cutover. Neither this policy commit
nor its tests authorize a role reset, a re-pair, or a data-volume deletion.
