(function observeClientNetwork() {
    const embedded = document.getElementById('frontierCloudNetworkConfig');
    let config = {};
    if (embedded) {
        try { config = JSON.parse(embedded.textContent) || {}; } catch (_error) { /* Invalid config is reported as disabled. */ }
    }
    const configuredUrls = embedded ? config.stun_urls : window.frontierCloudStunUrls;
    const urls = Array.isArray(configuredUrls)
        ? [...new Set(configuredUrls.filter(url => typeof url === 'string' && /^stun:(?:[a-z0-9.-]+|\[[0-9a-f:]+\]):\d{1,5}$/i.test(url)))] : [];
    const interval = Math.max(30000, Number(embedded ? config.interval_ms : window.frontierCloudWebrtcIntervalMs) || 30000);
    let busy = false;
    let stopped = false;
    let cancelPeer = null;
    let scheduled = null;
    let cancelReport = null;
    let visibilityEpoch = 0;

    async function report(addresses, failure = null) {
        window.frontierCloudObservedAddresses = [...addresses].slice(0, 8);
        window.dispatchEvent(new CustomEvent('frontiercloud-network-observation', {detail: {addresses: window.frontierCloudObservedAddresses, failure}}));
        const controller = new AbortController();
        let finish;
        const deadline = new Promise(resolve => { finish = resolve; });
        const abort = () => { controller.abort(); finish(); };
        cancelReport = abort;
        const timer = setTimeout(abort, 2000);
        try {
            await Promise.race([fetch('/api/v1/media/network-observation', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({addresses: [...addresses].slice(0, 8), failure}), signal: controller.signal,
            }), deadline]);
        } catch (_error) { /* Observation never blocks playback. */ }
        finally { clearTimeout(timer); cancelReport = null; }
    }

    function probeOne(url, addresses) {
        return new Promise(resolve => {
            let peer;
            let timer;
            let finished = false;
            let sawIceError = false;
            const finish = failure => {
                if (finished) return;
                finished = true;
                if (timer) clearTimeout(timer);
                if (peer) peer.close();
                cancelPeer = null;
                resolve(failure);
            };
            cancelPeer = () => finish('cancelled');
            try {
                // One URL in one live peer: browser ICE cannot fan out across nodes.
                peer = new RTCPeerConnection({iceServers: [{urls: url}], iceTransportPolicy: 'all'});
                peer.createDataChannel('network-observation');
                peer.addEventListener('icecandidate', event => {
                    if (!event.candidate) { finish(sawIceError ? 'ice_error' : 'no_srflx'); return; }
                    const candidate = event.candidate;
                    if (candidate.type !== 'srflx' && !/\styp\ssrflx(?:\s|$)/.test(candidate.candidate || '')) return;
                    const parts = String(candidate.candidate || '').split(/\s+/);
                    const address = candidate.address || parts[4];
                    if (address && addresses.size < 8) addresses.add(address);
                });
                peer.addEventListener('icecandidateerror', () => { sawIceError = true; });
                peer.addEventListener('icegatheringstatechange', () => {
                    if (peer.iceGatheringState === 'complete') finish(sawIceError ? 'ice_error' : 'no_srflx');
                });
                timer = setTimeout(() => finish('timeout'), 3000);
                peer.createOffer().then(offer => {
                    if (!finished) return peer.setLocalDescription(offer);
                }).catch(() => finish('ice_error'));
            } catch (_error) { finish('unsupported'); }
        });
    }

    async function sweep() {
        if (stopped || document.hidden) return;
        const epoch = visibilityEpoch;
        if (!urls.length) { await report([], 'disabled'); return; }
        if (typeof RTCPeerConnection !== 'function') { await report([], 'unsupported'); return; }
        const addresses = new Set();
        let failure = 'no_srflx';
        for (const url of urls) {
            if (stopped || document.hidden || epoch !== visibilityEpoch) return;
            failure = await probeOne(url, addresses);
            if (stopped || document.hidden || epoch !== visibilityEpoch) return;
            // Yield between nodes; no simultaneous candidate gathering or rounds.
            if (!stopped) await new Promise(resolve => setTimeout(resolve, 250));
        }
        if (!stopped && !document.hidden && epoch === visibilityEpoch) await report(addresses, addresses.size ? null : failure);
    }
    async function probe() {
        if (busy || stopped || document.hidden) return;
        busy = true;
        try {
            if (typeof navigator !== 'undefined' && navigator.locks) {
                // All tabs of this origin share one non-queued diagnostic lease.
                await navigator.locks.request('frontiercloud-stun-observation', {ifAvailable: true}, async lock => { if (lock) await sweep(); });
            } else { await sweep(); }
        } finally {
            busy = false;
            if (!stopped && !document.hidden) scheduled = setTimeout(probe, interval);
        }
    }
    if (window.addEventListener) {
        window.addEventListener('pagehide', () => {
            visibilityEpoch++;
            stopped = true;
            if (scheduled) clearTimeout(scheduled);
            if (cancelPeer) cancelPeer();
            if (cancelReport) cancelReport();
        });
        window.addEventListener('pageshow', event => {
            if (!event.persisted) return;
            stopped = false;
            if (scheduled) clearTimeout(scheduled);
            // Resume only after a cooldown; an older cancelled sweep must finish first.
            if (!busy) scheduled = setTimeout(probe, interval);
        });
    }
    if (document.addEventListener) {
        document.addEventListener('visibilitychange', () => {
            visibilityEpoch++;
            if (scheduled) clearTimeout(scheduled);
            if (document.hidden) {
                if (cancelPeer) cancelPeer();
                if (cancelReport) cancelReport();
            } else if (!stopped && !busy) {
                scheduled = setTimeout(probe, interval);
            }
        });
    }
    probe();
})();

(function loadTemporaryPlaybackContinuityDiagnostics() {
    const retireAt = Date.parse('2026-10-15T00:00:00Z');
    if (Date.now() >= retireAt || typeof PLAYER_KIND === 'undefined' || PLAYER_KIND !== 'audio') return;
    if (document.getElementById('frontierCloudPlaybackContinuityDiagnostics')) return;
    const script = document.createElement('script');
    script.id = 'frontierCloudPlaybackContinuityDiagnostics';
    script.src = '/static/js/playback-continuity-diagnostics.js';
    script.async = true;
    document.head.appendChild(script);
})();
