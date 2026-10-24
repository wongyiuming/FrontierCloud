(() => {
    'use strict';
    const link = document.querySelector('a.back-btn[href]');
    if (!link) return;
    const parent = new URL(link.href, location.href);
    if (parent.origin !== location.origin || !/^\/api\/v1\/media(?:\/(?:music|video)(?:\/category)?)?\/?$/.test(parent.pathname)) return;
    const cooldownKey = 'frontier:business-up:at';
    let touch = null, wheelDistance = 0, wheelAt = 0, navigating = false;
    const blocked = target => Boolean(target?.closest('button,input,select,textarea,[contenteditable],[role="slider"],dialog,.art-bottom,.art-controls,.fullscreen-lyrics'));
    const edge = x => x >= document.documentElement.clientWidth - 28;
    const consume = event => { if (event.cancelable) event.preventDefault(); event.stopPropagation(); };
    function up() {
        if (navigating) return;
        navigating = true;
        try { sessionStorage.setItem(cooldownKey, String(Date.now())); } catch (_) { /* Navigation also works without storage. */ }
        location.assign(parent.href);
    }
    // Reserve only the right edge. Interior album paging and player seeking
    // remain owned by their existing controls.
    document.addEventListener('pointerdown', event => {
        if (event.pointerType === 'touch' && edge(event.clientX) && !blocked(event.target)) event.stopPropagation();
    }, true);
    document.addEventListener('touchstart', event => {
        touch = null;
        if (event.touches.length !== 1 || blocked(event.target)) return;
        const first = event.touches[0];
        if (!edge(first.clientX)) return;
        touch = {id: first.identifier, x: first.clientX, y: first.clientY, at: event.timeStamp, claimed: false};
    }, {capture: true, passive: true});
    document.addEventListener('touchmove', event => {
        if (!touch) return;
        const current = Array.from(event.touches).find(item => item.identifier === touch.id);
        if (event.touches.length !== 1 || !current) { touch = null; return; }
        const dx = current.clientX - touch.x, dy = current.clientY - touch.y;
        if (Math.abs(dy) > 18 && Math.abs(dy) > Math.abs(dx)) { touch = null; return; }
        if (dx < -12 && Math.abs(dx) > Math.abs(dy) * 1.5) touch.claimed = true;
        if (touch.claimed) consume(event);
    }, {capture: true, passive: false});
    document.addEventListener('touchend', event => {
        const start = touch;
        touch = null;
        if (!start) return;
        const end = Array.from(event.changedTouches).find(item => item.identifier === start.id);
        if (!end || event.touches.length || !start.claimed) return;
        const dx = end.clientX - start.x, dy = end.clientY - start.y;
        if (dx <= -72 && Math.abs(dy) <= 48 && event.timeStamp - start.at <= 1200) { consume(event); up(); }
    }, {capture: true, passive: false});
    document.addEventListener('touchcancel', () => { touch = null; }, {capture: true, passive: true});

    function canScrollRight(target) {
        for (let item = target; item && item !== document.body && item !== document.documentElement; item = item.parentElement) {
            if (item.scrollWidth > item.clientWidth + 1 && /^(auto|scroll)$/.test(getComputedStyle(item).overflowX)
                && item.scrollLeft < item.scrollWidth - item.clientWidth - 1) return true;
        }
        return false;
    }
    // A trackpad swipe first belongs to any horizontal album scroller. When
    // it cannot scroll further, use the business parent instead of history.
    document.addEventListener('wheel', event => {
        if (event.ctrlKey || event.metaKey || event.defaultPrevented || blocked(event.target)
            || event.deltaX <= 0 || Math.abs(event.deltaX) <= Math.abs(event.deltaY) * 1.5 || canScrollRight(event.target)) {
            wheelDistance = 0;
            return;
        }
        consume(event);
        let previous = 0;
        try { previous = Number(sessionStorage.getItem(cooldownKey)) || 0; } catch (_) { /* No persistent cooldown available. */ }
        if (Date.now() - previous < 1200) return;
        if (event.timeStamp - wheelAt > 250) wheelDistance = 0;
        wheelAt = event.timeStamp;
        wheelDistance += event.deltaX * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? innerWidth : 1);
        if (wheelDistance >= 90) up();
    }, {passive: false});
    window.addEventListener('pageshow', () => { touch = null; wheelDistance = 0; wheelAt = 0; navigating = false; });
})();
