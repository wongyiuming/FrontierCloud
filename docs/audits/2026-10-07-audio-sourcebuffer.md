# Audio incident: deterministic track boundary and SourceBuffer race

## Scope and evidence

The reported public album contains a repeatable natural 07-to-08 failure.
Read-only production inspection found both tracks on Master Local, not a
Direct/Relay node. No media, database state, node relationship or production
deployment was changed in this investigation.

The deployed source was 915e1ec7148ee12bef71900269f85a398ec3b144. Its two
player scripts matched the pre-fix checkout. Both tracks decode as MPEG-1
Layer III, 48kHz stereo VBR. Full ffmpeg decoding of track 08 reported no error.
The files were copied privately to the development host with before/after
SHA-256 receipts; media bytes and credentials are not committed.

| Property | Track 07 | Track 08 |
| --- | ---: | ---: |
| File bytes | 10,566,956 | 8,292,716 |
| ffprobe duration | 316.040s | 242.093333s |
| Average bitrate | 267,449bps | 273,984bps |

Browser MP3 metadata/MSE duration differs slightly from ffprobe due to encoded
frame and padding accounting; the full browser segment for 08 is 242.135997s.

## Why shared code can repeatedly affect one track

There is no per-title parser, filename branch, hash blacklist or per-song
function. The shared MP3 pipeline receives different encoded frame sequences,
lengths and durations. A 512KiB batch contributes a content-dependent amount
of media time. Capacity checks are driven by the global media clock, and cleanup
is driven by the new active segment's start. Their relative timing depends on
the preceding track, current track bytes, fetch chunking and event scheduling.

Repeating the same pair under similar network conditions can repeatedly enter
the same narrow race window. Other songs may never enter that window under
the tested conditions; that does not prove they are universally immune.
Network speed changes the interleaving, not which parser function is selected.
Manual selection recreates MediaSource and starts at time zero, without the
previous track's range needing removal. This is why natural transition and
direct selection are not equivalent tests.

The old runtime-skip flag was also stored in the in-page catalog after failure,
making that song skipped for the remaining page session. It was not a durable
database song blacklist.

## Exact defective code

In static/js/audio-continuous-stream.js before the fix:

```javascript
await this.waitUpdateEnd();
await this.waitForAppendCapacity();
await this.appendBufferOnce(chunk);
```

The asynchronous capacity wait yields to timeupdate. That event can call
pruneBeforeActive and start sourceBuffer.remove after waitUpdateEnd returned.
The append then throws:

```text
InvalidStateError: Failed to execute 'appendBuffer' on 'SourceBuffer':
This SourceBuffer is still processing an 'appendBuffer' or 'remove' operation.
```

The appendTrack catch incorrectly treated every read/append failure as a
partially completed song:

```javascript
catch (error) {
    if (this.closed || session !== this) throw error;
    segment.partial = true;
}
segment.end = this.bufferedEnd();
// Later: markRuntimeSkip(index, interruption warning), then return true.
```

That is the code that converted a browser write failure into an early business
track boundary. A network-only retry shim cannot fix an append/remove race.

## Repair

appendBufferOnce and pruneBeforeActive now submit their entire operations to
serializeSourceBuffer, including their updateend waits:

```javascript
const next = previous.catch(() => {}).then(async () => {
    if (this.closed || session !== this) throw new Error('stale session');
    await this.waitUpdateEnd();
    return operation();
});
this.sourceBufferOperation = next.catch(() => {});
```

appendTrack no longer swallows runtime errors or marks a partial track complete.
An initialized pipeline error holds the current session with diagnostic state;
it never starts another song. Runtime skip flags and the yellow interruption
warning have been removed. Format incompatibility labels remain a separate,
static MP3-only profile contract.

Network failures continue to wait/retry silently. Resume validates 206 and
Content-Range at the exact delivered offset, total length and strong ETag when
present; If-Range prevents concatenating bytes of a changed object. Rejected
responses/readers are cancelled, retries have bounded backoff, and a deliberate
user switch aborts in-flight work. No retry limit automatically advances a song.

## Controlled browser evidence

Tests use isolated headless Chromium and the same original files, with the
underlying buffered media clock advanced to accelerate the experiment. They do
not call business track selection to cross the natural boundary.

| Experiment | 08 appended duration | Runtime warning/error |
| --- | ---: | --- |
| Original code, natural 07-to-08 | 32.064s | append/remove InvalidStateError and early-next warning |
| Original code, disable range pruning only | 242.135997s | none |
| Original code, select 08 first | 242.135997s | none |
| Fixed code, natural 07-to-08 | 242.135997s | none |

The pruning-disabled experiment is causal isolation, not a proposed fix:
permanently disabling cleanup would grow memory during long playlists.

The fault-injection browser test disconnects 08 after 131072 delivered bytes,
serves temporary 503s for eight seconds, observes the unfinished active track,
and verifies exact-offset recovery and complete duration with no warning/skip.
Expected browser network console messages are retained separately from pipeline
errors. Unit smoke tests also cover overlapping append/prune, decoder hold,
HTTP outage, Range base offsets, object identity mismatch, abort and stale
generation cancellation.

These tests establish the observed browser race and policy behavior. They do
not claim a mobile-provider packet trace, physical-device background/suspension
coverage, or that every reported song interruption has this single cause.
Actual mobile 07-to-08 playback must be checked after an authorized deployment.

## Reproduction and release boundary

Run outside hosted CI, with Playwright and Chrome installed in the test driver:

```bash
node tests/audio_continuous_browser.mjs --fixtures /private/audio-fixtures \
  --baseline --baseline-ref 5d334cb963e07213d410f5b297124f8608c4ee06
# Repeat with --no-prune or --manual as causal controls.
node tests/audio_continuous_browser.mjs --fixtures /private/audio-fixtures
node tests/audio_continuous_browser.mjs --fixtures /private/audio-fixtures --disconnect
node tests/audio_continuous_stream_smoke.mjs
node tests/audio_continuous_fetch_retry_smoke.mjs
```

Supply private files named 07.mp3 and 08.mp3. Optional --playwright and --browser
arguments select driver dependencies. JSON evidence is written beside those
private fixtures. Do not commit media or install these tools into Web.

The playback fix and Go-only/FastAPI retirement are separate commits. This
report is not a production deployment receipt. Deployment must bind both Web
and Nginx static assets to the tested revision; existing browser tabs must reload
to fetch the new content-hashed scripts.
