# Playback Continuity

Background music continuation is a core FrontierCloud business contract. The current design is music-only and uses one browser media session instead of handing playback from one media element to another.

## Continuous audio model

For compatible music, FrontierCloud uses:

```text
one audio player
-> one MediaSource
-> one audio/mpeg SourceBuffer
-> SourceBuffer.mode = sequence
-> A | B | C | ...
```

Crossing a song boundary changes the visible track, lyrics, metadata, and accounting, but it does not intentionally change `src`, call `load()`, or start a second media element.

The old T-200 ms warm-standby/bridge/takeover design is retired. It must not be restored as a fallback behind the continuous-stream core.

## Supported continuous format

The first compatibility profile is deliberately strict: MP3 delivered as `audio/mpeg`, with browser MSE support for that MIME type.

Existing M4A, FLAC, and WAV resources are not rewritten. They remain available for manual single-track playback, are marked in the playlist as continuous-stream incompatible, and are skipped by automatic continuation.

FrontierCloud performs no hidden transcoding. If a file is normalized by an external tool or service, the administrator uploads the normalized result through the normal Admin workflow.

## Resource limits

The browser streams existing resource responses into the SourceBuffer. FrontierCloud does not build a physical album file and does not create a server-side continuous-media cache.

The initial implementation keeps only a small logical window: the active compatible track plus one look-ahead compatible track. Old buffered ranges are removed after the next track becomes active.

No FFmpeg, runtime transcoder, new derived-media volume, or continuous-media database is part of this feature.

## Media-folder placement affinity

Admin still asks only for `primary`, `direct`, or `relay`; it never asks for a concrete storage member.

The immediate parent folder of a media file is now the placement-affinity unit. The first upload into an empty folder uses the existing fair placement algorithm. Later direct-child media files in that same folder stay on the same physical member.

Nested media folders are independent placement units. For example:

```text
music/Artist/Disc-1/* -> Direct A
music/Artist/Disc-2/* -> Direct B
```

The first upload into `Disc-2` can independently choose the least-pressured ready Direct member.

A bound folder never spills to another member when its owner is offline, read-only, missing, or full. The upload fails instead. Changing site type for an already-bound folder also fails.

Historical folders that are already split across several members are not migrated automatically. New uploads into such folders fail closed until the historical placement is intentionally resolved.

## Video isolation

Video is not part of this continuity architecture. Video pages do not load the audio continuous-stream core. Their normal end-of-media behavior remains unchanged.

## Failure behavior

If the browser does not support the MP3 MSE profile, or a continuous session cannot initialize, FrontierCloud falls back to the existing single-track audio path. This degradation must never make otherwise playable media unavailable.

If an individual candidate track cannot enter the continuous buffer, that track is marked/skipped and the user receives a visible notice.

## Release gates

CI protects the following core rules:

- no T-200 ms handoff, standby Deck, or bridge remains;
- one audio MediaSource and one sequence SourceBuffer implement compatible continuity;
- no normal `endOfStream()` boundary is created between songs;
- continuous playback is MP3-only in the first profile;
- incompatible audio is visibly skipped for auto-next but remains manually playable;
- video does not load the audio continuity module;
- immediate-parent folder affinity is enforced for new uploads;
- nested folders are independently balanced;
- a bound folder cannot spill to another member.

See `docs/audio-continuous-stream.md` in the repository for the complete engineering contract.
