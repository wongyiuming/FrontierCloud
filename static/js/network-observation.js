(function observeClientNetwork() {
    const urls = Array.isArray(window.frontierCloudStunUrls)
        ? window.frontierCloudStunUrls.filter(url => typeof url === 'string' && /^stuns?:/.test(url)).slice(0, 4)
        : [];
    let sent = false;

    async function report(addresses, failure = null) {
        if (sent) return;
        sent = true;
        try {
            await fetch('/api/v1/media/network-observation', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({addresses: [...addresses].slice(0, 8), failure}),
                keepalive: true,
            });
        } catch (_error) {
            // Observation is diagnostic only and never blocks the page.
        }
    }

    if (!urls.length) {
        report([], 'disabled');
        return;
    }
    if (typeof RTCPeerConnection !== 'function') {
        report([], 'unsupported');
        return;
    }

    const addresses = new Set();
    let peer;
    let timer;
    let finished = false;
    let sawIceError = false;
    const finish = failure => {
        if (finished) return;
        finished = true;
        if (timer) clearTimeout(timer);
        if (peer) peer.close();
        report(addresses, addresses.size ? null : failure);
    };

    try {
        peer = new RTCPeerConnection({
            iceServers: urls.map(url => ({urls: url})),
            iceTransportPolicy: 'all',
        });
        peer.createDataChannel('network-observation');
        peer.addEventListener('icecandidate', event => {
            if (!event.candidate) {
                finish(sawIceError ? 'ice_error' : 'no_srflx');
                return;
            }
            const candidate = event.candidate;
            if (candidate.type !== 'srflx' && !/\styp\ssrflx(?:\s|$)/.test(candidate.candidate || '')) return;
            const parts = String(candidate.candidate || '').split(/\s+/);
            const address = candidate.address || parts[4];
            if (address) addresses.add(address);
        });
        peer.addEventListener('icecandidateerror', () => {
            // ICE errors are per server/address-family attempt. A later attempt
            // can still produce a valid srflx candidate, so wait for completion.
            sawIceError = true;
        });
        peer.addEventListener('icegatheringstatechange', () => {
            if (peer.iceGatheringState === 'complete') {
                finish(sawIceError ? 'ice_error' : 'no_srflx');
            }
        });
        timer = setTimeout(() => finish('timeout'), 8000);
        peer.createOffer()
            .then(offer => peer.setLocalDescription(offer))
            .catch(() => finish('ice_error'));
    } catch (_error) {
        finish('unsupported');
    }
})();
