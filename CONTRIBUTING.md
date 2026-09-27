# Contributing to FrontierCloud

FrontierCloud intentionally uses a two-branch delivery model. These rules are architectural constraints, not suggestions.

## Repository topology — MUST NOT violate

- `dev` is the **only development branch**. All implementation, tests, documentation, and conflict-resolution commits go directly to the existing `dev` branch.
- `main` is the **release branch**. The only valid pull request targeting `main` is a same-repository `dev -> main` promotion.
- **Do not create any new branch.** This includes `feature/*`, `fix/*`, temporary conflict-resolution branches, release branches, experiment branches, or automation-created branches.
- Do not open a feature/fix branch directly against `main`.
- Do not force-push or rewrite `dev` or `main`.
- Do not commit implementation work directly to `main`.
- After a `dev -> main` release PR is merged, fast-forward `dev` to the resulting `main` merge commit before the next implementation commit. This keeps the two histories linear even when GitHub creates a merge commit for the release PR.
- Historical non-canonical branches may exist until the repository owner deletes them. They are not implementation targets and must not be reused.

The repository can fail an invalid PR topology, but repository-local code cannot reliably prevent somebody with GitHub ref permission from creating a branch. The **no-new-branch rule therefore remains an explicit human/automation invariant** and should also be mirrored in the GitHub Wiki and repository ruleset/branch-protection settings.

## Change discipline

1. Read `ARCHITECTURE.md` before changing a cross-cutting subsystem.
2. Start from the current `dev` HEAD. If `main` was just promoted, synchronize `dev` to `main` first as described above.
3. Change the smallest coherent surface. Do not revive retired compatibility/product features to make a test pass.
4. Add or strengthen regression coverage for every bug fix and every new invariant.
5. Run the complete `dev` CI. A passing narrow unit test is not release evidence.
6. Open exactly one release PR from `dev` to `main`. The repository owner performs the merge.

## Regression policy

Prefer executable protection over prose:

- Business invariants belong in unit/runtime tests.
- Browser behavior and Admin layout belong in Chromium acceptance tests when practical.
- Master/Follower behavior belongs in federation tests when it crosses node boundaries.
- Nginx/Compose/release assumptions belong in source/deployment contract tests.
- A rule that cannot be reliably asserted from the repository (for example, **never create a new Git branch**) must be documented here and in the Wiki instead of being implied by convention.

Never weaken an existing regression merely to make a new implementation pass. If an invariant genuinely changes, update the architecture documentation and the test in the same change and explain why.

## Non-negotiable architecture boundaries

- A cluster has one business Master. Followers are resource nodes, not independent business authorities.
- Master-owned business state (lyrics, lyric relations, playback/business facts, users, audit facts) must not silently migrate to Followers.
- One managed media object is complete and belongs to exactly one storage member. Do not split one logical object across storage members.
- Compute Worker is retired. Do not add worker slots, compute scheduling, worker UI, or worker product configuration back without an explicit architecture decision.
- Heartbeat health is independent from Backup work. Backup failure must never mark a healthy node offline.
- Backup is a recovery artifact, not online replication, HA, or automatic failover.
- Managed paths are changed only through FrontierCloud transactions. Do not rename/move/delete managed files directly on disk as a substitute for metadata updates.
- Folder rename is a rename inside the same parent, not a general move API. Cross-member rename must remain rollback-capable. Master rename is the exclusive path mutation; upload reservation, delete, hide/unhide, and priority changes must participate in the same mutation-fence protocol.
- Lyrics support at most two directory levels below `lyrics`: `lyrics/<category>/<subdir>/<file>.lrc`. Every upload, catalog, tree, search, download, and delete surface must enforce the same boundary.
- `lyrics/default.lrc` is an internal playback fallback. It is not user content and must not appear in Admin lists/counts/search or be exposed as a normal mutable object.
- Same-name lyric auto-link is fallback automation: it may replace missing/default fallback state but **must never overwrite an explicit user-managed lyric relation**, even if that selected lyric file is temporarily unavailable.
- Admin module order is intentional and protected by regression tests. Do not reorder modules incidentally while changing a module.

## Release evidence

A release is valid only when the exact `dev` commit being promoted has successful CI and the resulting `main` tree is identical to the reviewed `dev` tree. Post-merge release provenance checks are an additional guard, not a replacement for the `dev -> main` workflow.
