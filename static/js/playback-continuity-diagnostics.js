(function temporaryPlaybackContinuityDiagnostics() {
    'use strict';

    const RETIRE_AT = Date.parse('2026-10-15T00:00:00Z');
    const ENDPOINT = '/api/v1/media/playback-continuity-diagnostics';
    const MAX_TIMELINE = 20;
    const WATCHDOG_MS = 1000;
    const PLAY_START_TIMEOUT_MS = 8000;
    const STALL_TIMEOUT_MS = 5000;
    const TRANSITION_TIMEOUT_MS = 20000;
    const LOOP_GAP_MS = 3500;
    const CONFIRMED_PROGRESS_SECONDS = 5;

    if (!Number.isFinite(RETIRE_AT) || Date.now() >= RETIRE_AT) return;
    if (typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio') return;

    let video = null;
    let transition = null;
    let lastIndex = null;
    let lastWatch = performance.now();
    let lastProgressAt = performance.now();
    let lastProgressTime = 0;
    let timeline = [];

    const safeNumber = value => Number.isFinite(Number(value)) ? Number(value) : null;
    const short = (value, limit = 256) => String(value ?? '').slice(0, limit);

    function currentTrack(index = null) {
        const selected = index === null ? (typeof currentIndex === 'number' ? currentIndex : -1) : index;
        const item = typeof currentMediaList !== 'undefined' && Array.isArray(currentMediaList)
            ? currentMediaList[selected] : null;
        if (!item) return {index: selected};
        return {
            index: selected,
            media_id: short(item.media_id || item.resource_id || '', 128),
            media_path: short(item.media_path || '', 512),
            title: short(item.title || '', 256),
            type: short(item.type || '', 32),
        };
    }

    function bufferedEnd(media) {
        try {
            return media?.buffered?.length ? media.buffered.end(media.buffered.length - 1) : null;
        } catch (_error) {
            return null;
        }
    }

    function autoplayPolicy() {
        try {
            return typeof navigator.getAutoplayPolicy === 'function'
                ? short(navigator.getAutoplayPolicy('mediaelement'), 64) : null;
        } catch (_error) {
            return null;
        }
    }

    function snapshot() {
        const media = video;
        const error = media?.error;
        const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
        let playRequested = null;
        try {
            if (typeof art !== 'undefined' && art) {
                playRequested = typeof art.playRequested === 'boolean'
                    ? art.playRequested
                    : (typeof art._playRequested === 'boolean' ? art._playRequested : null);
            }
        } catch (_error) {}
        return {
            monotonic_ms: Math.round(performance.now()),
            wall_ms: Date.now(),
            index: typeof currentIndex === 'number' ? currentIndex : null,
            switch_sequence: typeof playerSwitchSequence === 'number' ? playerSwitchSequence : null,
            visibility: document.visibilityState,
            hidden: Boolean(document.hidden),
            focused: typeof document.hasFocus === 'function' ? document.hasFocus() : null,
            was_discarded: Boolean(document.wasDiscarded),
            online: navigator.onLine,
            media_session: navigator.mediaSession?.playbackState || null,
            autoplay_policy: autoplayPolicy(),
            play_requested: playRequested,
            playing: media ? (!media.paused && !media.ended) : null,
            paused: media?.paused ?? null,
            ended: media?.ended ?? null,
            seeking: media?.seeking ?? null,
            current_time: safeNumber(media?.currentTime),
            duration: safeNumber(media?.duration),
            ready_state: media?.readyState ?? null,
            network_state: media?.networkState ?? null,
            buffered_end: safeNumber(bufferedEnd(media)),
            playback_rate: safeNumber(media?.playbackRate),
            muted: media?.muted ?? null,
            volume: safeNumber(media?.volume),
            error: error ? {code: error.code, message: short(error.message, 256)} : null,
            connection: connection ? {
                effective_type: short(connection.effectiveType, 32),
                downlink: safeNumber(connection.downlink),
                rtt: safeNumber(connection.rtt),
                save_data: Boolean(connection.saveData),
            } : null,
        };
    }

    function note(event, detail = null) {
        timeline.push({event, at_ms: Date.now(), state: snapshot(), detail});
        if (timeline.length > MAX_TIMELINE) timeline = timeline.slice(-MAX_TIMELINE);
    }

    function clientInfo() {
        const data = navigator.userAgentData;
        return {
            user_agent: short(navigator.userAgent, 512),
            platform: short(navigator.platform, 128),
            language: short(navigator.language, 64),
            brands: Array.isArray(data?.brands)
                ? data.brands.slice(0, 8).map(item => ({brand: short(item.brand, 64), version: short(item.version, 32)}))
                : [],
            mobile: data?.mobile ?? null,
            ua_platform: short(data?.platform, 64),
        };
    }

    function send(stage, reason = '') {
        if (Date.now() >= RETIRE_AT) return;
        const payload = {
            diagnostic_id: transition?.id || `ambient-${Date.now().toString(36)}`,
            stage,
            reason,
            sent_at_ms: Date.now(),
            client: clientInfo(),
            track: currentTrack(),
            sample: snapshot(),
            timeline: timeline.slice(-MAX_TIMELINE),
        };
        const body = JSON.stringify(payload);
        try {
            if (navigator.sendBeacon) {
                const blob = new Blob([body], {type: 'application/json'});
                if (navigator.sendBeacon(ENDPOINT, blob)) return;
            }
        } catch (_error) {}
        try {
            fetch(ENDPOINT, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body,
                keepalive: true,
                cache: 'no-store',
                credentials: 'same-origin',
            }).catch(() => {});
        } catch (_error) {}
    }

    function beginTransition(previousIndex) {
        transition = {
            id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`,
            previous_index: previousIndex,
            started_at: performance.now(),
            next_selected_at: null,
            playing_at: null,
            playing_time: null,
            reported_no_start: false,
            reported_stall: false,
            reported_pause: false,
            reported_gap: false,
        };
        note('transition_started', {previous_track: currentTrack(previousIndex)});
        send('transition_started', 'natural_end_observed');
    }

    function closeTransition(stage, reason) {
        if (!transition) return;
        note(stage, {reason});
        send(stage, reason);
        transition = null;
    }

    function onMediaEvent(event) {
        const eventName = event.type;
        const observedIndex = typeof currentIndex === 'number' ? currentIndex : null;
        const previousIndex = lastIndex;
        note(`media:${eventName}`);

        if (eventName === 'ended') {
            beginTransition(previousIndex === null ? observedIndex : previousIndex);
        }
        if (!transition) return;

        if (observedIndex !== null && observedIndex !== transition.previous_index && transition.next_selected_at === null) {
            transition.next_selected_at = performance.now();
            note('next_track_selected', {track: currentTrack(observedIndex)});
            send('next_track_selected', 'index_changed_after_end');
        }
        if (eventName === 'play') send('next_play_event', 'media_play_event');
        if (eventName === 'playing') {
            transition.playing_at = performance.now();
            transition.playing_time = safeNumber(video?.currentTime) ?? 0;
            lastProgressAt = performance.now();
            lastProgressTime = safeNumber(video?.currentTime) ?? 0;
            send('next_track_playing', 'media_playing_event');
        }
        if (eventName === 'waiting' || eventName === 'stalled') {
            send('next_track_buffering', `media_${eventName}`);
        }
        if (eventName === 'error') closeTransition('failure_media_error', 'html_media_error');
        if (eventName === 'pause' && transition.playing_at !== null && !video?.ended && !transition.reported_pause) {
            transition.reported_pause = true;
            send('failure_pause_after_next_started', document.hidden ? 'paused_while_backgrounded' : 'paused_after_next_started');
        }
    }

    function watchdog() {
        const now = performance.now();
        const gap = now - lastWatch;
        lastWatch = now;

        if (typeof currentIndex === 'number') {
            if (lastIndex !== null && currentIndex !== lastIndex) note('index_changed', {from: lastIndex, to: currentIndex});
            lastIndex = currentIndex;
        }

        if (!transition || !video) return;
        if (gap > LOOP_GAP_MS && !transition.reported_gap) {
            transition.reported_gap = true;
            note('event_loop_gap', {gap_ms: Math.round(gap)});
            send('event_loop_gap', `watchdog_gap_${Math.round(gap)}ms`);
        }

        const elapsed = now - transition.started_at;
        const current = safeNumber(video.currentTime) ?? 0;
        if (!video.paused && current > lastProgressTime + 0.05) {
            lastProgressTime = current;
            lastProgressAt = now;
        }

        if (transition.playing_at === null && elapsed >= PLAY_START_TIMEOUT_MS && !transition.reported_no_start) {
            transition.reported_no_start = true;
            send('failure_autoplay_not_started', 'no_playing_event_before_deadline');
        }

        if (transition.playing_at !== null) {
            const progressed = current - (transition.playing_time ?? current);
            if (progressed >= CONFIRMED_PROGRESS_SECONDS) {
                closeTransition('transition_progress_confirmed', 'next_track_progressed');
                return;
            }
            if (!video.paused && now - lastProgressAt >= STALL_TIMEOUT_MS && !transition.reported_stall) {
                transition.reported_stall = true;
                send('failure_no_progress', 'playing_without_current_time_progress');
            }
        }

        if (elapsed >= TRANSITION_TIMEOUT_MS) closeTransition('transition_unresolved', 'transition_deadline_reached');
    }

    function attach(candidate) {
        if (!candidate || candidate === video) return;
        video = candidate;
        lastIndex = typeof currentIndex === 'number' ? currentIndex : null;
        lastProgressTime = safeNumber(video.currentTime) ?? 0;
        lastProgressAt = performance.now();
        for (const name of [
            'ended', 'play', 'playing', 'pause', 'waiting', 'stalled', 'error', 'abort', 'emptied',
            'loadstart', 'loadedmetadata', 'canplay', 'suspend', 'seeking', 'seeked', 'ratechange',
        ]) video.addEventListener(name, onMediaEvent, {passive: true});
        note('diagnostics_attached');
    }

    for (const name of ['visibilitychange', 'freeze', 'resume', 'pagehide', 'pageshow']) {
        const target = name === 'visibilitychange' ? document : window;
        target.addEventListener(name, event => {
            note(`page:${event.type}`);
            if (transition) send('page_state_changed', event.type);
        }, {passive: true});
    }

    window.addEventListener('online', event => { note(`network:${event.type}`); if (transition) send('network_state_changed', event.type); }, {passive: true});
    window.addEventListener('offline', event => { note(`network:${event.type}`); if (transition) send('network_state_changed', event.type); }, {passive: true});

    const attachTimer = setInterval(() => {
        if (Date.now() >= RETIRE_AT) {
            clearInterval(attachTimer);
            return;
        }
        try {
            if (typeof art !== 'undefined' && art?.video) attach(art.video);
        } catch (_error) {}
    }, 250);
    setInterval(watchdog, WATCHDOG_MS);
})();
