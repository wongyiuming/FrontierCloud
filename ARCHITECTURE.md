# FrontierCloud Architecture Boundaries

This document records invariants that must survive feature work. It is intentionally stricter than a feature list: contributors should understand **what FrontierCloud is allowed to become** before changing cross-cutting code.

## 1. Roles and authority

FrontierCloud runs as one of three roles:

- **Standalone** — one node owns business state and local media.
- **Master** — the only business authority in a cluster. Public pages and business APIs are served by the Master.
- **Follower** — a resource node paired to a Master. A Follower provides authenticated health, storage/data-plane, backup, and node-control capabilities; it is not a second business authority.

A cluster has exactly one business Master. Do not introduce multi-Master, implicit leader election, automatic Master failover, or peer-to-peer business writes under an unrelated feature change.

Fixed Master/Follower roles require certificate-verified HTTPS. Loss of the TLS requirement must fail closed rather than silently downgrading a fixed role.

## 2. Business state versus resource state

The Master owns business truth, including lyrics and lyric relations, playback/business facts, karaoke users/recording business state, Admin audit facts, and the global media catalog.

Followers own only the resource state required to serve their assigned data and maintain the authenticated relationship with the Master. A Follower must not become an alternate source of truth for Master business records.

## 3. Storage model

The Master exposes one logical storage pool containing Master Local plus enabled Followers.

- One logical media path identifies one complete media object.
- One complete object belongs to exactly one storage member at a time.
- Placement is transparent to playback/download: Local, Direct, and Relay are transport choices, not different business objects.
- Storage allocation is FrontierCloud logical capacity. Physical disk capacity is observed separately.
- Admin Storage wording is intentionally split into `physical used / all` and `used / allocated`; do not conflate filesystem free space with FrontierCloud quota.

### Upload site types

Admin media upload chooses a **site type**, never a concrete storage member. The selector starts empty and media upload does not begin until a type is explicitly selected.

The only supported site types are:

- `primary` / 主站 — the Master Local placement (`member_kind=MasterLocal`, transport `Local`);
- `direct` / 直连站点 — Follower placements whose current transport is `Direct`;
- `relay` / 中继站点 — Follower placements whose current transport is `Relay`.

For Direct and Relay uploads, the user must not name a specific member. FrontierCloud selects among all members of the requested type that are storage-enabled, online, writable, and have enough writable capacity for the object. Placement prefers the lowest current `(used + reserved) / allocated` pressure and then more available bytes, so sequential/concurrent reservations spread naturally across ready members instead of sticking to one node. Member selection plus durable upload reservation is serialized by the storage write lock so concurrent Admin sessions see current `reserved_bytes`.

Historical media requires **no migration** for this feature. Existing `storage_member_id`, `member_kind`, and `transport` remain the source of truth; Admin derives the visible site type from those fields. A missing transport in a local/Standalone media tree is treated as primary/local. Changing this presentation must not rewrite historical ownership.

Site-type colors in Admin are observation aids only: local/primary uses a muted green marker, Direct a muted amber marker, and Relay a muted red marker. They must remain low-saturation badges rather than full-row alert colors.

Compute Worker is **retired**. Worker slots, leased compute scheduling, worker UI, or product-level Compute configuration must not be reintroduced accidentally through compatibility code.

## 4. Heartbeat and Backup

Heartbeat is node-health control traffic. Backup is asynchronous recovery work. They are independent failure domains:

- a successful heartbeat stays successful even if Backup subsequently fails;
- Backup work must not block the heartbeat loop;
- full Backup construction is serialized to avoid multiple large in-memory business backups being built concurrently;
- interrupted receiving generations are explicitly aborted/cleaned when supported.

Backup artifacts are bounded business-recovery packages. They are **not** online replicas, HA storage, or automatic failover.

## 5. Managed media mutation

Managed filesystem paths and database metadata form one business object lifecycle. Direct filesystem changes are not a supported substitute for FrontierCloud mutations.

Deletion uses the recovery journal/quarantine transaction path. Rename must update both bytes and metadata or roll back.

### Folder rename

Folder rename is deliberately narrower than a general move API:

- only supported managed `music`/`vido` folders are rename targets;
- the parent directory does not change;
- target collisions are rejected;
- active uploads and pending deletion state block rename;
- a Master coordinates all storage members containing that logical folder;
- if a later member or Master metadata commit fails, already moved members are rolled back in reverse order;
- offline Followers block a rename that cannot be completed safely.

All Master path mutations participate in one process-local reader/writer fence. Cross-member folder rename owns the exclusive fence from preflight through commit/rollback. Upload-session reservation, global delete, hide/unhide, and priority mutations enter through a shared fence; upload reservations and `pending_delete` then remain durable database fences after that short shared section ends. A new path-mutation entry point must join this protocol instead of creating an independent race window.

Do not turn folder rename into an arbitrary cross-parent move without a new transaction design and federation regression coverage.

## 6. Directory priority

Folders are first-class sorting objects. Directory priority is distinct from media-file priority and uses the same bounded preference range.

Public category/subcategory ordering is `directory priority descending -> name`. Priority state is stored as managed metadata and must migrate with a successful folder rename.

Catalog ordering is cached server-side. A cache hit must not cause a new MySQL sort query; changing priority or a directory name must invalidate the catalog generation.

## 7. Lyrics

Lyrics are Master-owned business content even when media bytes are stored on Followers.

The supported hierarchy is intentionally bounded:

```text
lyrics/<file>.lrc
lyrics/<category>/<file>.lrc
lyrics/<category>/<subdir>/<file>.lrc
```

Two directories below `lyrics` is the maximum. Upload, validation, Admin tree, scoped search, download/delete collection, lyric catalog, and relation management **must all enforce the same hierarchy**. It is forbidden for one surface to accept a path that another surface cannot manage.

`lyrics/default.lrc` is an internal, automatically maintained playback fallback:

- it is not user lyric content;
- it is excluded from Admin lyric counts and relation counts;
- it is excluded from Admin media-tree/search presentation;
- it cannot be selected as a normal delete/download target;
- a fallback relation does not make a track appear to have a user-managed lyric in Admin.

Same-name auto-link is fallback automation, not an authority over user decisions. It may fill a missing relation or replace `lyrics/default.lrc`, but it **must never overwrite an explicit user-managed lyric relation**, even when that selected lyric file is temporarily unavailable. The current-relation check and replacement happen under the same database row lock so a concurrent manual link cannot be overwritten. Auto-link must scan only the supported media and lyric hierarchy; historical orphan files outside that hierarchy cannot re-enter business state through automation.

An accepted lyric upload is successful only after the file and its managed-object registration are durable together. A failed registration/audit transaction removes the newly published file. A later cache-invalidation failure must never delete an already committed lyric.

## 8. Catalog and cache consistency

Redis catalog generations cache expensive scans/sorts. Browser/API cache headers must not make mutable directory structure remain stale after a successful Admin mutation.

For mutable catalog APIs, the HTTP client revalidates while the server-side Redis generation remains the expensive-work cache. Mutations that change paths, visibility, priority, or catalog membership invalidate the generation. Cache invalidation is fail-soft after a durable business commit: a Redis failure is logged and TTL recovery remains available; it must not turn a committed mutation into a false client-visible failure.

## 9. Admin GUI contract

The Admin console order is an intentional product contract and is tested in both source and real Chromium layout:

1. Media management
2. Media sorting policy
3. Lyric relations
4. Karaoke users
5. IP security
6. WebRTC network relations
7. Nodes
8. Site access state
9. System release management
10. Admin Key
11. Brand Logo

Dynamic modules must join this same final DOM/visual order. Reordering code must be idempotent; DOM mutation observers must not create self-triggering reorder loops.

## 10. Release topology

The repository has two canonical branches:

- `dev` — implementation and complete CI authority;
- `main` — reviewed release history.

Absolute repository policy:

- **Do not create any new branch.** Additional feature/fix/release/temporary branches are prohibited.
- all changes go to the existing `dev`;
- the only PR into `main` is same-repository `dev -> main`;
- after the release PR is merged, fast-forward `dev` to the resulting `main` commit before further work;
- never force-rewrite `dev` or `main`.

GitHub-side ref creation cannot be fully prevented by a unit test, so the no-new-branch invariant must remain documented here, in `CONTRIBUTING.md`, and in the Wiki/ruleset configuration.

## 11. Regression rule

When an invariant can be encoded as a test, encode it. When it cannot be reliably observed from repository code (for example, who is allowed to create a Git ref), document it explicitly and enforce it with GitHub repository settings where available.

A regression may not be weakened merely to accommodate a new feature. Architectural changes require an explicit architecture update and corresponding test changes in the same development cycle.
