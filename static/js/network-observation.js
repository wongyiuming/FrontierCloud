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
    const finish = failure => {
        if (timer) clearTimeout(timer);
        if (peer) peer.close();
        report(addresses, addresses.size ? null : failure);
    };

    try {
        peer = new RTCPeerConnection({iceServers: [{urls}]});
        peer.createDataChannel('network-observation');
        peer.addEventListener('icecandidate', event => {
            if (!event.candidate) {
                finish('no_srflx');
                return;
            }
            const candidate = event.candidate;
            if (candidate.type !== 'srflx' && !/\styp\ssrflx(?:\s|$)/.test(candidate.candidate || '')) return;
            const parts = String(candidate.candidate || '').split(/\s+/);
            const address = candidate.address || parts[4];
            if (address) addresses.add(address);
        });
        peer.addEventListener('icecandidateerror', () => {
            if (!addresses.size) finish('ice_error');
        }, {once: true});
        timer = setTimeout(() => finish('timeout'), 5000);
        peer.createOffer()
            .then(offer => peer.setLocalDescription(offer))
            .catch(() => finish('ice_error'));
    } catch (_error) {
        finish('unsupported');
    }
})();
