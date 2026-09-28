(function bootstrapPlaybackContinuityHandoff() {
    'use strict';

    const HANDOFF_SECONDS = 0.2;
    const SYSTEM_DEDUP_MS = 900;
    const STANDBY_POLL_MS = 250;
    const MIN_READY_STATE = 3;
    const MAIN_TAKEOVER_TIMEOUT_MS = 8000;
    const MAIN_PROGRESS_CONFIRM_MS = 500;
    const MAIN_PROGRESS_MIN_SECONDS = 0.05;
    const DIAGNOSTIC_RETIRE_AT = Date.parse('2026-10-15T00:00:00Z');
    const DIAGNOSTIC_ENDPOINT = '/api/v1/media/playback-continuity-diagnostics';

    function installWhenReady() {
        if (typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio') return;
        if (typeof playNext !== 'function'
                || typeof playPrev !== 'function'
                || typeof selectMedia !== 'function'
                || typeof checkAndPreloadNext !== 'function'
                || typeof discardNextPreload !== 'function'
                || typeof updateMediaSession !== 'function') {
            window.setTimeout(installWhenReady, 50);
            return;
        }
        if (window.frontierCloudPlaybackContinuityHandoff?.installed) return;
        install();
    }

    function install() {
        const originalPlayNext = playNext;
        const originalPlayPrev = playPrev;
        const originalSelectMedia = selectMedia;
        const originalCheckAndPreloadNext = checkAndPreloadNext;
        const originalDiscardNextPreload = discardNextPreload;
        const originalUpdateMediaSession = updateMediaSession;

        let standby = null;
        let handoffTimer = null;
        let handoffTimerDue = null;
        let handoffInFlight = false;
        let systemGuardUntil = 0;
        let pendingSource = null;
        let pollTimer = null;
        let activeDiagnosticId = null;
        let handoffGeneration = 0;
        let activeTakeover = null;

        const now = () => performance.now();
        const safeNumber = value => Number.isFinite(Number(value)) ? Number(value) : null;
        const short = (value, limit = 128) => String(value ?? '').slice(0, limit);

        function currentTrack(index = currentIndex) {
            const item = Array.isArray(currentMediaList) ? currentMediaList[index] : null;
            return item ? {
                index,
                media_id: short(item.media_id || item.resource_id || '', 128),
                type: short(item.type || '', 32),
            } : {index};
        }

        function remainingSeconds() {
            const duration = safeNumber(art?.duration);
            const current = safeNumber(art?.currentTime);
            if (duration === null || current === null || duration <= 0) return null;
            return Math.max(0, duration - current);
        }

        function remainingWallSeconds() {
            const remaining = remainingSeconds();
            const rate = safeNumber(art?.video?.playbackRate);
            if (remaining === null) return null;
            return remaining / (rate !== null && rate > 0 ? rate : 1);
        }

        function stateSnapshot(extra = {}) {
            const main = art?.video || null;
            return {
                handoff_seconds: HANDOFF_SECONDS,
                current_index: typeof currentIndex === 'number' ? currentIndex : null,
                switch_sequence: typeof playerSwitchSequence === 'number' ? playerSwitchSequence : null,
                remaining_seconds: remainingSeconds(),
                remaining_wall_seconds: remainingWallSeconds(),
                visibility: document.visibilityState,
                hidden: Boolean(document.hidden),
                main_paused: main?.paused ?? null,
                main_ended: main?.ended ?? null,
                main_ready_state: main?.readyState ?? null,
                main_network_state: main?.networkState ?? null,
                main_current_time: safeNumber(main?.currentTime),
                standby_ready: Boolean(standby?.ready),
                standby_ready_state: standby?.element?.readyState ?? null,
                standby_current_time: safeNumber(standby?.element?.currentTime),
                handoff_in_flight: handoffInFlight,
                handoff_generation: handoffGeneration,
                takeover_switch_sequence: activeTakeover?.switchSequence ?? null,
                ...extra,
            };
        }

        function report(stage, reason, extra = {}) {
            if (!Number.isFinite(DIAGNOSTIC_RETIRE_AT) || Date.now() >= DIAGNOSTIC_RETIRE_AT) return;
            const diagnosticId = activeDiagnosticId || `handoff-${Date.now().toString(36)}`;
            const payload = {
                diagnostic_id: diagnosticId,
                stage,
                reason,
                sent_at_ms: Date.now(),
                client: {
                    user_agent: short(navigator.userAgent, 512),
                    platform: short(navigator.platform, 128),
                    language: short(navigator.language, 64),
                },
                track: currentTrack(),
                sample: stateSnapshot(extra),
                timeline: [{
                    event: stage,
                    at_ms: Date.now(),
                    state: stateSnapshot(extra),
                    detail: extra,
                }],
            };
            const body = JSON.stringify(payload);
            try {
                if (navigator.sendBeacon) {
                    const blob = new Blob([body], {type: 'application/json'});
                    if (navigator.sendBeacon(DIAGNOSTIC_ENDPOINT, blob)) return;
                }
            } catch (_error) {}
            try {
                fetch(DIAGNOSTIC_ENDPOINT, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body,
                    keepalive: true,
                    cache: 'no-store',
                    credentials: 'same-origin',
                }).catch(() => {});
            } catch (_error) {}
        }

        function dispatchTrackRequest(source, direction, accepted, detail = {}) {
            const payload = {source, direction, accepted, at_ms: Date.now(), ...detail};
            try {
                window.dispatchEvent(new CustomEvent('frontiercloud:track-change-request', {detail: payload}));
            } catch (_error) {}
            report('track_change_requested', accepted ? 'accepted' : 'deduplicated', payload);
        }

        function isSystemSource(source) {
            return source === 'media-session-next'
                || source === 'media-session-prev'
                || source === 'media-key-next'
                || source === 'media-key-prev';
        }

        function allowTrackRequest(source, direction) {
            const accepted = !isSystemSource(source) || now() >= systemGuardUntil;
            dispatchTrackRequest(source, direction, accepted, {
                guard_remaining_ms: Math.max(0, Math.round(systemGuardUntil - now())),
            });
            return accepted;
        }

        function clearHandoffTimer() {
            if (handoffTimer !== null) window.clearTimeout(handoffTimer);
            handoffTimer = null;
            handoffTimerDue = null;
        }

        function clearTakeoverMonitor(record = activeTakeover) {
            if (!record) return;
            if (record.timer !== null) window.clearTimeout(record.timer);
            if (record.progressTimer !== null) window.clearTimeout(record.progressTimer);
            art?.off?.('video:playing', record.listener);
            if (activeTakeover === record) activeTakeover = null;
        }

        function cleanupStandby({preserveObjectUrl = false} = {}) {
            clearHandoffTimer();
            if (!standby) return;
            const candidate = standby;
            standby = null;
            try { candidate.element.pause(); } catch (_error) {}
            try { candidate.element.remove(); } catch (_error) {}
            if (!preserveObjectUrl
                    && candidate.objectUrl
                    && candidate.objectUrl !== activeObjectUrl) {
                try { URL.revokeObjectURL(candidate.objectUrl); } catch (_error) {}
            }
        }

        function standbyMatchesCurrentNext() {
            if (!standby || !Array.isArray(currentMediaList) || currentMediaList.length <= 1) return false;
            const expectedIndex = nextMediaIndex();
            const next = currentMediaList[expectedIndex];
            return standby.preparedFromIndex === currentIndex
                && standby.index === expectedIndex
                && standby.url === next?.url;
        }

        function schedulePreEndHandoff() {
            if (!standby?.ready || handoffInFlight || !standbyMatchesCurrentNext()) {
                clearHandoffTimer();
                return;
            }
            if (!art?.video || art.video.paused || art.video.ended) {
                clearHandoffTimer();
                return;
            }
            const remaining = remainingWallSeconds();
            if (remaining === null) return;
            if (remaining <= HANDOFF_SECONDS) {
                clearHandoffTimer();
                void performPreEndHandoff('threshold');
                return;
            }
            const delay = Math.max(0, (remaining - HANDOFF_SECONDS) * 1000);
            const due = now() + delay;
            if (handoffTimer !== null && handoffTimerDue !== null && Math.abs(handoffTimerDue - due) < 80) return;
            clearHandoffTimer();
            handoffTimerDue = due;
            handoffTimer = window.setTimeout(() => {
                handoffTimer = null;
                handoffTimerDue = null;
                const latest = remainingWallSeconds();
                if (latest !== null && latest > HANDOFF_SECONDS + 0.03) {
                    schedulePreEndHandoff();
                    return;
                }
                void performPreEndHandoff('timer');
            }, delay);
        }

        function markStandbyReady(candidate) {
            if (standby !== candidate || candidate.ready) return;
            candidate.ready = true;
            report('preend_standby_ready', 'standby_canplay', {
                target_index: candidate.index,
                prepared_from_index: candidate.preparedFromIndex,
            });
            schedulePreEndHandoff();
        }

        function prepareStandby(entry) {
            if (!art?.video || !entry?.objectUrl || entry.status !== 'ready') return;
            const targetIndex = nextMediaIndex();
            const next = currentMediaList?.[targetIndex];
            if (!next || next.type !== 'audio' || next.url !== entry.url) return;
            if (standby?.objectUrl === entry.objectUrl
                    && standby.index === targetIndex
                    && standby.preparedFromIndex === currentIndex) {
                if (standby.ready) schedulePreEndHandoff();
                return;
            }
            if (!handoffInFlight) cleanupStandby();
            if (handoffInFlight) return;

            const element = document.createElement('audio');
            element.preload = 'auto';
            element.playsInline = true;
            element.setAttribute('playsinline', '');
            element.setAttribute('webkit-playsinline', '');
            element.setAttribute('aria-hidden', 'true');
            element.tabIndex = -1;
            element.style.position = 'absolute';
            element.style.width = '1px';
            element.style.height = '1px';
            element.style.opacity = '0';
            element.style.pointerEvents = 'none';
            element.volume = Number.isFinite(Number(art.video.volume)) ? Number(art.video.volume) : 0.7;
            element.muted = Boolean(art.video.muted);
            element.playbackRate = Number.isFinite(Number(art.video.playbackRate)) ? Number(art.video.playbackRate) : 1;

            const candidate = {
                element,
                objectUrl: entry.objectUrl,
                url: entry.url,
                index: targetIndex,
                preparedFromIndex: currentIndex,
                ready: false,
                bridgeActive: false,
            };
            standby = candidate;
            const ready = () => markStandbyReady(candidate);
            element.addEventListener('canplay', ready, {once: true});
            element.addEventListener('error', () => {
                if (standby !== candidate || candidate.bridgeActive) return;
                report('preend_standby_failed', 'standby_media_error', {target_index: candidate.index});
                cleanupStandby();
            }, {once: true});
            element.src = entry.objectUrl;
            (art.root || document.body || document.documentElement).appendChild(element);
            element.load();
            if (element.readyState >= MIN_READY_STATE) ready();
        }

        function observeStandby() {
            if (handoffInFlight) return;
            if (standby && !standbyMatchesCurrentNext()) cleanupStandby();
            if (nextPreload?.status === 'ready' && nextPreload.objectUrl) prepareStandby(nextPreload);
            if (standby?.ready) schedulePreEndHandoff();
        }

        function restoreMainDeckSettings(mainVideo, userAudio) {
            if (!mainVideo || !userAudio) return;
            mainVideo.volume = userAudio.volume;
            mainVideo.muted = userAudio.muted;
            mainVideo.playbackRate = userAudio.playbackRate;
        }

        function restoreMainDeckAudio(mainVideo, userAudio, bridge) {
            if (!mainVideo || !bridge) return;
            const bridgeTime = safeNumber(bridge.currentTime);
            const mainTime = safeNumber(mainVideo.currentTime);
            if (bridgeTime !== null && mainTime !== null && bridgeTime > 0.05 && Math.abs(bridgeTime - mainTime) > 0.08) {
                try { mainVideo.currentTime = bridgeTime; } catch (_error) {}
            }
            restoreMainDeckSettings(mainVideo, userAudio);
        }

        function scheduleMainRecovery(record) {
            if (!record || activeTakeover !== record) return;
            if (record.timer !== null) window.clearTimeout(record.timer);
            record.timer = window.setTimeout(() => {
                record.timer = null;
                const current = activeTakeover === record
                    && record.generation === handoffGeneration
                    && standby === record.candidate
                    && record.candidate.bridgeActive;
                if (!current) {
                    clearTakeoverMonitor(record);
                    return;
                }
                if (record.switchSequence !== null
                        && (currentIndex !== record.targetIndex
                            || playerSwitchSequence !== record.switchSequence)) {
                    report('preend_handoff_superseded', 'newer_track_selection', {
                        target_index: record.targetIndex,
                        generation: record.generation,
                    });
                    record.candidate.bridgeActive = false;
                    handoffInFlight = false;
                    try { record.candidate.element.pause(); } catch (_error) {}
                    restoreMainDeckSettings(art?.video, record.userAudio);
                    clearTakeoverMonitor(record);
                    cleanupStandby({preserveObjectUrl: true});
                    activeDiagnosticId = null;
                    return;
                }
                report('preend_main_deck_pending', 'main_not_playing_before_deadline', {
                    target_index: record.targetIndex,
                    generation: record.generation,
                });
                if (art?.video && !art.video.paused && !art.video.ended
                        && Number(art.video.currentTime) > 0.05) {
                    record.listener();
                    return;
                }
                try {
                    if (record.switchSequence === null) {
                        pendingSource = 'pre-end-recovery';
                        originalSelectMedia(record.targetIndex);
                        record.switchSequence = playerSwitchSequence;
                        if (art?.video) art.video.muted = true;
                        pendingSource = null;
                    }
                    Promise.resolve(art?.play?.()).catch(error => {
                        report('preend_main_deck_retry_failed', short(error?.name || 'play_failed', 64), {
                            target_index: record.targetIndex,
                            generation: record.generation,
                        });
                    });
                } catch (error) {
                    pendingSource = null;
                    report('preend_main_deck_retry_failed', short(error?.name || 'play_failed', 64), {
                        target_index: record.targetIndex,
                        generation: record.generation,
                    });
                }
                scheduleMainRecovery(record);
            }, MAIN_TAKEOVER_TIMEOUT_MS);
        }

        async function performPreEndHandoff(trigger) {
            if (handoffInFlight || !standby?.ready || !standbyMatchesCurrentNext()) return false;
            const remaining = remainingWallSeconds();
            if (remaining === null || remaining > HANDOFF_SECONDS + 0.03 || !art?.video || art.video.paused || art.video.ended) return false;

            const candidate = standby;
            const oldVideo = art.video;
            const oldIndex = currentIndex;
            const targetIndex = candidate.index;
            const generation = ++handoffGeneration;
            const userAudio = {
                volume: Number.isFinite(Number(oldVideo.volume)) ? Number(oldVideo.volume) : 0.7,
                muted: Boolean(oldVideo.muted),
                playbackRate: Number.isFinite(Number(oldVideo.playbackRate)) ? Number(oldVideo.playbackRate) : 1,
            };
            activeDiagnosticId = `handoff-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
            handoffInFlight = true;
            clearHandoffTimer();
            candidate.element.volume = userAudio.volume;
            candidate.element.muted = userAudio.muted;
            candidate.element.playbackRate = userAudio.playbackRate;
            report('preend_handoff_started', 't_minus_200ms', {trigger, old_index: oldIndex, target_index: targetIndex});

            try {
                await candidate.element.play();
            } catch (error) {
                handoffInFlight = false;
                report('preend_handoff_failed', short(error?.name || 'play_failed', 64), {trigger, target_index: targetIndex});
                activeDiagnosticId = null;
                return false;
            }

            if (currentIndex !== oldIndex || art.video !== oldVideo || !standbyMatchesCurrentNext()) {
                try { candidate.element.pause(); } catch (_error) {}
                handoffInFlight = false;
                report('preend_handoff_aborted', 'player_state_changed', {target_index: targetIndex});
                activeDiagnosticId = null;
                return false;
            }

            candidate.bridgeActive = true;
            const record = {
                candidate,
                generation,
                listener: null,
                switchSequence: null,
                targetIndex,
                timer: null,
                progressTimer: null,
                userAudio,
            };
            const takeover = () => {
                if (standby !== candidate || !candidate.bridgeActive || activeTakeover !== record) return;
                if (record.switchSequence === null) return;
                if (generation !== handoffGeneration
                        || currentIndex !== targetIndex
                        || playerSwitchSequence !== record.switchSequence) {
                    candidate.bridgeActive = false;
                    handoffInFlight = false;
                    try { candidate.element.pause(); } catch (_error) {}
                    restoreMainDeckSettings(art?.video, userAudio);
                    clearTakeoverMonitor(record);
                    cleanupStandby({preserveObjectUrl: true});
                    report('preend_handoff_superseded', 'newer_track_playing', {target_index: targetIndex, generation});
                    activeDiagnosticId = null;
                    return;
                }
                const mainVideo = art?.video;
                if (!mainVideo || mainVideo.paused || mainVideo.ended || record.progressTimer !== null) return;
                const progressStart = safeNumber(mainVideo.currentTime);
                if (progressStart === null) return;
                record.progressTimer = window.setTimeout(() => {
                    record.progressTimer = null;
                    if (standby !== candidate || !candidate.bridgeActive || activeTakeover !== record) return;
                    if (generation !== handoffGeneration
                            || currentIndex !== targetIndex
                            || playerSwitchSequence !== record.switchSequence
                            || art?.video !== mainVideo) {
                        takeover();
                        return;
                    }
                    const progressEnd = safeNumber(mainVideo.currentTime);
                    const progressed = !mainVideo.paused
                        && !mainVideo.ended
                        && progressEnd !== null
                        && progressEnd - progressStart >= MAIN_PROGRESS_MIN_SECONDS;
                    if (!progressed) {
                        report('preend_main_deck_stalled', 'playing_event_without_clock_progress', {
                            target_index: targetIndex,
                            generation,
                            progress_start: progressStart,
                            progress_end: progressEnd,
                        });
                        if (record.timer === null) scheduleMainRecovery(record);
                        return;
                    }
                    restoreMainDeckAudio(mainVideo, userAudio, candidate.element);
                    candidate.bridgeActive = false;
                    handoffInFlight = false;
                    try { candidate.element.pause(); } catch (_error) {}
                    clearTakeoverMonitor(record);
                    cleanupStandby({preserveObjectUrl: true});
                    report('preend_main_deck_resumed', 'main_clock_progressed_after_bridge', {
                        target_index: targetIndex,
                        generation,
                        progress_seconds: progressEnd - progressStart,
                    });
                    activeDiagnosticId = null;
                }, MAIN_PROGRESS_CONFIRM_MS);
            };
            record.listener = takeover;
            activeTakeover = record;
            art.on?.('video:playing', takeover);
            candidate.element.addEventListener('ended', () => {
                if (activeTakeover !== record || standby !== candidate || !candidate.bridgeActive) return;
                if (art?.video && !art.video.paused && !art.video.ended
                        && Number(art.video.currentTime) > 0.05) {
                    takeover();
                    return;
                }
                report('preend_bridge_ended', 'main_never_took_over', {target_index: targetIndex, generation});
                candidate.bridgeActive = false;
                handoffInFlight = false;
                clearTakeoverMonitor(record);
                cleanupStandby({preserveObjectUrl: true});
                activeDiagnosticId = null;
                if (currentIndex === targetIndex) playNext('bridge-ended-fallback');
            }, {once: true});

            try {
                oldVideo.pause();
                oldVideo.muted = true;
                pendingSource = 'pre-end-200ms';
                dispatchTrackRequest(pendingSource, 'next', true, {target_index: targetIndex, trigger});
                originalSelectMedia(targetIndex);
                if (art?.video) art.video.muted = true;
                record.switchSequence = playerSwitchSequence;
                scheduleMainRecovery(record);
                systemGuardUntil = now() + SYSTEM_DEDUP_MS;
                report('preend_bridge_active', 'standby_playing_before_old_end', {target_index: targetIndex, trigger, generation});
            } catch (error) {
                pendingSource = null;
                report('preend_main_switch_failed', short(error?.name || error?.message || 'switch_failed', 128), {target_index: targetIndex, generation});
                scheduleMainRecovery(record);
                return true;
            } finally {
                pendingSource = null;
            }

            return true;
        }

        checkAndPreloadNext = function continuityAwarePreload(currentTime) {
            const result = originalCheckAndPreloadNext(currentTime);
            observeStandby();
            return result;
        };

        discardNextPreload = function continuityAwareDiscard() {
            if (!handoffInFlight) cleanupStandby();
            return originalDiscardNextPreload();
        };

        selectMedia = function continuityAwareSelect(index) {
            const source = pendingSource || 'direct-select';
            dispatchTrackRequest(source, index >= currentIndex ? 'next-or-select' : 'previous-or-select', true, {target_index: index});
            return originalSelectMedia(index);
        };

        playNext = function continuityAwareNext(source = 'legacy-next') {
            if (!allowTrackRequest(source, 'next')) return false;
            pendingSource = source;
            try { return originalPlayNext(); }
            finally { pendingSource = null; }
        };

        playPrev = function continuityAwarePrevious(source = 'legacy-prev') {
            if (!allowTrackRequest(source, 'previous')) return false;
            pendingSource = source;
            try { return originalPlayPrev(); }
            finally { pendingSource = null; }
        };

        updateMediaSession = function continuityAwareMediaSession(media) {
            const result = originalUpdateMediaSession(media);
            if ('mediaSession' in navigator) {
                navigator.mediaSession.setActionHandler('previoustrack', () => { playPrev('media-session-prev'); });
                navigator.mediaSession.setActionHandler('nexttrack', () => { playNext('media-session-next'); });
            }
            return result;
        };

        window.addEventListener('keydown', event => {
            if (event.key === 'MediaTrackNext' || event.code === 'MediaTrackNext') {
                event.preventDefault();
                event.stopImmediatePropagation();
                playNext('media-key-next');
            } else if (event.key === 'MediaTrackPrevious' || event.code === 'MediaTrackPrevious') {
                event.preventDefault();
                event.stopImmediatePropagation();
                playPrev('media-key-prev');
            }
        }, true);

        window.addEventListener('pagehide', () => {
            clearHandoffTimer();
            if (pollTimer !== null) window.clearInterval(pollTimer);
            pollTimer = null;
            if (!handoffInFlight) cleanupStandby();
        }, {passive: true});

        window.frontierCloudPlaybackContinuityHandoff = {
            installed: true,
            handoffSeconds: HANDOFF_SECONDS,
            status() {
                return stateSnapshot({
                    system_guard_remaining_ms: Math.max(0, Math.round(systemGuardUntil - now())),
                    bridge_active: Boolean(standby?.bridgeActive),
                });
            },
        };

        if (typeof art !== 'undefined' && art && Array.isArray(currentMediaList) && currentMediaList[currentIndex]) {
            updateMediaSession(currentMediaList[currentIndex]);
        }
        pollTimer = window.setInterval(observeStandby, STANDBY_POLL_MS);
        observeStandby();
    }

    installWhenReady();
})();
