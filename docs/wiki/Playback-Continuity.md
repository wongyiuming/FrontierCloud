# Playback Continuity

Background audio auto-next is a core FrontierCloud business contract.

## Why the handoff exists

Tesla Chromium traces showed that the currently playing track can continue normally while the browser is backgrounded, but the renderer can stop progressing during the short interval after the old media pipeline ends and before the new media pipeline reaches `loadedmetadata` / `canplay` / `playing`.

FrontierCloud therefore avoids creating that empty interval whenever the next audio track can be prepared in advance.

## Warm standby flow

The next audio object keeps the existing bounded preload path, then also warms a separate standby media element.

The required sequence is:

```text
current Deck playing
-> next Blob preload completes
-> standby element loads next track
-> standby reaches readyState >= 3 / canplay
-> remaining time reaches 200 ms
-> standby.play() succeeds
-> old Deck pauses
-> main Deck selects the next track while muted
-> main Deck reaches playing
-> main Deck synchronizes to bridge position
-> user volume/mute/rate state is restored
-> standby stops
```

The **200 ms** threshold is a deliberate safety margin for embedded Chromium scheduling jitter. Do not move the transition back to natural `ended` without new real-device evidence and regression coverage.

## Ended fallback

The early path must fail soft. If standby preparation or standby `play()` fails, the old track remains untouched and the ordinary natural **ended fallback** is still responsible for selecting the next track.

This means a failed optimization must never create a worse playback interruption than the legacy path.

## Duplicate system controls

A successful pre-end handoff starts a 900 ms guard for duplicate system-originated track commands from MediaSession or media-key events. Playlist selection and ordinary player controls are not removed.

Track-change source information is sent to the temporary playback continuity diagnostics while that diagnostic interface remains enabled.

## Lifetime boundaries

The warm standby handoff is permanent core behavior.

The Tesla diagnostic API is temporary, memory-only, and independently retired. Removing the diagnostic endpoint must not remove the permanent handoff module.

## Release gates

The contract is protected by:

- `tests/playback_handoff_smoke.mjs`, which exercises T-200 ms standby-first handoff, takeover, duplicate system-control suppression, and ended fallback;
- `tests/network_observation_smoke.mjs`, which runs that handoff regression and validates this Wiki page, the MD core contract, and the Wiki sidebar directly from the checkout before image build;
- `tests/test_playback_continuity_core_contract.py`, which verifies the permanent runtime code contract inside the Web image;
- the normal Docker Compose, Chromium UI, HTTPS cluster, source, and runtime release gates.

Any change to the 200 ms threshold, standby-first ordering, ended fallback, or system-control deduplication requires matching test and documentation changes.
