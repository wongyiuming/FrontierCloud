# Playback Continuity Core Contract

Background audio continuity is a core FrontierCloud business invariant. The product must preserve automatic next-track playback across foreground, background, minimized, occluded, and embedded browser lifecycle states whenever the browser still permits an already-playing media session to continue.

## Incident model

Tesla Chromium behavior observed after an OTA showed a repeatable boundary:

```text
current track keeps playing in background
-> current track reaches natural end
-> next source is selected
-> play is requested
-> media waits at readyState 0 / networkState loading
-> renderer or media loading work can stop making progress
```

Windows Chromium did not reproduce the failure. The important application-side risk is therefore the empty interval between an ended old media pipeline and a not-yet-playing new pipeline.

## Permanent continuity design

Audio playback uses a warm standby bridge for the next track.

1. Existing speculative preload remains bounded by `PRELOAD_MAX_BYTES`.
2. When the next preload becomes a complete Blob URL, a separate standby media element receives that URL and calls `load()` while the current track is still playing.
3. The standby is considered warm only at `readyState >= 3` (`HAVE_FUTURE_DATA`) or after `canplay`.
4. The permanent handoff threshold is **200 ms before natural end**.
5. At T-200 ms, FrontierCloud calls `standby.play()` while the old Deck is still actively playing.
6. The old Deck is paused only after standby playback succeeds.
7. The normal player then selects the same next track with the main Deck muted while it catches the already-playing bridge.
8. When the main Deck emits `video:playing`, it is synchronized to the bridge position, the user's volume/mute/playback-rate state is restored, and the standby is stopped.
9. The next cycle then prepares the following track.

The ordering is intentional:

```text
standby warm
-> standby.play succeeds
-> old Deck pauses
-> main Deck selects next track
-> main Deck playing
-> bridge relinquishes audio
```

Never change this to:

```text
old Deck ends or pauses
-> load next media
-> then try to play
```

That recreates the lifecycle gap that triggered the Tesla failure.

## Ended fallback

The T-200 ms path is opportunistic, not destructive.

If the standby is missing, not warm, or its `play()` call fails, FrontierCloud must leave the current track untouched. The existing natural `ended -> playNext()` path remains the ended fallback. Early-handoff failure must never pause the old track or advance business state.

Video playback is not changed by this contract.

## Media-control deduplication

Some embedded systems can emit more than one system media-control signal for one physical transition. After a successful pre-end handoff, FrontierCloud applies a short 900 ms guard to system-originated controls:

- MediaSession next track;
- MediaSession previous track;
- `MediaTrackNext`;
- `MediaTrackPrevious`.

The guard does not remove the normal playlist, player gesture, or ended fallback paths. Track-change sources are also reported through the temporary continuity diagnostics while that diagnostic interface is alive.

## Diagnostic separation

The continuity fix is permanent. The Tesla diagnostic API is temporary.

The core loader has no retirement date. The diagnostic endpoint, memory ring, and client probe retain their separate hard retirement date and short TTL. Removing the temporary diagnostics must not remove `playback-continuity-handoff.js` or weaken this contract.

## Regression requirements

The following regressions are release gates:

- `tests/playback_handoff_smoke.mjs` verifies T-200 ms warm standby behavior, standby-first ordering, main-deck takeover, system-event deduplication, and early-failure fallback.
- `tests/network_observation_smoke.mjs` imports that smoke test, so the existing frontend CI gate executes it.
- `tests/test_playback_continuity_core_contract.py` protects the permanent loader, the 200 ms threshold, source deduplication, fallback behavior, and documentation.
- the normal runtime test discovery executes the Python contract in CI.

A change to these semantics is an architecture change and must update implementation, regression coverage, this document, and the Wiki in the same `dev` cycle.
