# Audio Continuous Stream Core Contract

Audio background continuity is a core FrontierCloud business capability. This contract applies only to the music player. Video playback keeps its existing single-resource lifecycle and may stop normally at media end.

## Goal

A compatible music directory should play through one browser media session instead of repeatedly ending one media resource and starting another.

The browser model is:

```text
one FrontierAudioPlayer
-> one underlying media element
-> one MediaSource
-> one SourceBuffer in sequence mode
-> track A bytes | track B bytes | track C bytes | ...
```

A track boundary is business metadata, not a browser media-session boundary. Crossing from one compatible track to the next must not require a new `src`, `load()`, or `play()` call.

## Retired design

The previous pre-end handoff design is retired and must not be reintroduced:

- no T-200 ms timer;
- no second standby media element;
- no audible bridge;
- no standby-to-main takeover;
- no measured-clock takeover probe;
- no takeover recovery loop.

The temporary playback-continuity diagnostics are independent from this permanent architecture and may be removed on their own retirement schedule.

## Compatibility profile

The first continuous-stream profile is intentionally narrow:

```text
MIME: audio/mpeg
file extension: .mp3
MSE requirement: MediaSource.isTypeSupported('audio/mpeg')
SourceBuffer mode: sequence
```

FrontierCloud continues to accept the existing music upload formats. A non-MP3 audio object remains manually playable through the legacy single-track path, but it does not join automatic continuous playback. The playlist visibly marks it as incompatible and auto-next skips it.

This is deliberate. FrontierCloud does not silently transcode, remux, rewrite, or replace an uploaded media object.

If an administrator later normalizes an incompatible file with an external tool or service, the normalized file must be uploaded again through the existing Admin upload workflow. The bytes accepted by Admin remain the bytes stored by FrontierCloud.

## Resource model

The Web service does not create a physical concatenated media file and does not create a server-side derived-media cache.

The browser fetches each existing media URL and streams response bytes into the same SourceBuffer. The continuous-stream core must not add:

- FFmpeg to the production Web image;
- runtime transcoding;
- a derived-media volume;
- a continuous-media database table;
- a server-side concatenation endpoint.

The implementation keeps a bounded playback window with two logical tracks: the current compatible track and one look-ahead compatible track. Appends are backpressured to at most 30 seconds ahead of the media clock, and old SourceBuffer ranges are removed after playback has crossed into the new active track. A browser `QuotaExceededError` pauses the append pipeline until playback frees capacity, then retries the same bytes silently; it must never mark the track as interrupted or skip it.

A per-track byte guard remains in place so one malformed or unexpectedly large response cannot cause unbounded JavaScript buffering work.

## Playback performance invariants

MSE is the production playback path for compatible MP3 and must preserve the old player's responsiveness.

The following are P0 release contracts:

- the visible duration for the active business track is stable; it must never use the currently buffered MSE range as the track's total duration;
- the visible duration is a write-once presentation value: once MP3 metadata/response length yields a valid estimate it is latched for that track and later SourceBuffer growth or completed-segment duration must not overwrite it;
- if no valid early estimate is available, the presentation duration remains unknown until the completed segment can provide the one allowed fallback value; the UI must never fall back to `HTMLMediaElement.duration` or `MediaSource.duration` while an MSE session is active;
- network chunks are accumulated into bounded append batches instead of issuing one `SourceBuffer.appendBuffer()` / `updateend` cycle for every fetch chunk;
- SourceBuffer capacity is bounded by playback-clock backpressure, and quota pressure retries the same append without changing playlist state or displaying a warning;
- the first append is intentionally small enough for fast startup, while later appends are larger to reduce main-thread and SourceBuffer churn;
- normal MSE fetches may use the browser HTTP cache and must not force `cache: no-store`;
- an all-compatible MP3 catalog must not perform a second per-row DOM decoration scan after the normal playlist render;
- compatibility warning DOM work is performed only when an incompatible/runtime-skipped item actually exists.

The current first-append target is 64 KiB and the regular append batch target is 512 KiB. These are implementation constants and may be tuned only together with browser regression tests.

## Track boundary semantics

When the underlying global MSE time crosses into the next appended segment, FrontierCloud updates business state only:

- `currentIndex`;
- active playlist row;
- title;
- synchronized lyrics;
- MediaSession metadata;
- playback accounting.

The player UI exposes local track time even though the underlying media element uses one monotonically increasing MSE timeline.

Normal compatible-track transitions do not call `MediaSource.endOfStream()` and do not intentionally create a media `ended` boundary.

## Manual controls

Manual selection of a compatible MP3 starts a continuous session from that track.

Manual selection of an incompatible audio file remains available and uses the existing single-track player. Automatic next/previous selection skips incompatible tracks while continuous audio is supported by the browser.

If the MSE session itself cannot initialize, FrontierCloud fails soft to the existing single-track player rather than making all audio unavailable.

## Media-folder placement affinity

Admin still selects only a site type:

```text
primary
direct
relay
```

The operator never selects a concrete Storage member.

For new uploads, the immediate parent media folder is the smallest placement-affinity unit. The first live reservation in an empty folder uses the existing fair placement algorithm within the selected site type. Once that reservation exists, every direct child media file in the same folder is pinned to that physical storage member.

Example:

```text
music/Artist/Disc-1/01.mp3
music/Artist/Disc-1/02.mp3
```

Both files must share one storage member.

A child media folder is a new independent affinity unit:

```text
music/Artist/Disc-1/* -> Direct Follower A
music/Artist/Disc-2/* -> Direct Follower B
```

`Disc-2` is free to choose the least-pressured ready Direct member when its first upload is reserved.

Affinity is derived from existing `global_media_objects` plus live, unexpired upload reservations. No separate folder-to-node table is introduced.

A bound folder never silently spills to another same-type member. If its owner is offline, read-only, missing, or too full, the upload fails. A request using a different site type from the existing folder owner also fails.

Historical folders already split across more than one storage member are not migrated by this change. New uploads into such a split folder fail closed so the inconsistency does not grow.

## Storage and upload invariants

This feature does not change FrontierCloud's storage ownership model:

- every managed media object still has exactly one physical owner;
- every media upload still enters through the existing Admin business workflow;
- the uploaded object is stored without hidden format conversion;
- Local, Direct, and Relay transport remain authoritative per resource;
- the Master global catalog remains authoritative for placement.

The folder-affinity rule is an upload-placement constraint, not a new storage service.

## Video boundary

The continuous-stream script is loaded only by the audio player template. The video player must not install the MSE audio core, must not inherit audio compatibility skipping, and must not change its end-of-media behavior because of this feature.

## Release gates

The release must protect at least the following:

- the retired T-200 ms/standby/bridge implementation is absent;
- the continuous core installs only for `PLAYER_KIND === 'audio'`;
- the profile remains `audio/mpeg` unless implementation, tests, and documentation are changed together;
- the SourceBuffer uses `sequence` mode;
- normal continuous playback does not call `endOfStream()`;
- no second audio media element is created by the continuity core;
- active-track presentation duration is write-once and never grows with `bufferedEnd()`, `HTMLMediaElement.duration`, or `MediaSource.duration`;
- MSE writes are batched rather than one append per fetch chunk;
- all-MP3 playlists take the no-extra-decoration fast path;
- incompatible formats are visibly marked and skipped by automatic continuation;
- manual single-track fallback remains available;
- media-folder affinity uses the immediate parent only;
- a bound folder cannot spill to another member when its owner becomes unavailable;
- child folders independently use normal fair placement on their first reservation;
- video pages do not load the audio continuous-stream core.

The normal source tests, runtime tests, real Chromium UI regression, multi-node HTTPS acceptance, Compose gates, security checks, and Admin/Public flow tests remain mandatory.
