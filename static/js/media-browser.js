(() => {
    'use strict';

    const CACHE_PREFIX = 'frontier:catalog:v1:';
    const MAX_CACHE_BYTES = 256 * 1024;
    const HIDDEN_REVEAL_CLICK_LIMIT = 15;
    const HIDDEN_REVEAL_WINDOW_MS = 60 * 1000;
    const artworkJobs = new WeakMap();
    const artworkCache = new Map();
    const ARTWORK_CONCURRENCY = 3, ARTWORK_CACHE_LIMIT = 32, ARTWORK_CACHE_MS = 30000;
    let artworkRunning = 0, artworkCursor = 0, artworkPage = null, renderedCategories = '';

    function lookupArtwork(url) {
        const cached = artworkCache.get(url);
        if (cached && Date.now() - cached.at < ARTWORK_CACHE_MS) return cached.promise;
        if (artworkCache.size >= ARTWORK_CACHE_LIMIT) artworkCache.delete(artworkCache.keys().next().value);
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        const entry = {at: Date.now()};
        entry.promise = (async () => {
            for (let attempt = 0; attempt < 2; attempt++) {
                controller.signal.throwIfAborted();
                const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
                if (response.ok) return response.json();
                if (response.status === 401 || response.status === 403) artworkCache.clear();
                const retry = response.status === 503 ? response.headers?.get('Retry-After') : null;
                await response.body?.cancel();
                const seconds = typeof retry === 'string' && /^\d+$/.test(retry) ? Number(retry) : NaN;
                if (attempt !== 0 || !Number.isInteger(seconds) || seconds < 1 || seconds >= 8)
                    throw new Error('Artwork candidates unavailable');
                await new Promise((resolve, reject) => {
                    const abortDelay = () => {
                        clearTimeout(timer);
                        controller.signal.removeEventListener('abort', abortDelay);
                        reject(controller.signal.reason || new Error('Artwork lookup canceled'));
                    };
                    const timer = setTimeout(() => {
                        controller.signal.removeEventListener('abort', abortDelay);
                        resolve();
                    }, seconds * 1000);
                    controller.signal.addEventListener('abort', abortDelay, {once: true});
                    if (controller.signal.aborted) abortDelay();
                });
            }
        })().catch(error => { if (artworkCache.get(url) === entry) artworkCache.delete(url); throw error; })
            .finally(() => clearTimeout(timeout));
        artworkCache.set(url, entry);
        return entry.promise;
    }

    function hydrateVisibleArtwork() {
        const grid = document.getElementById('categoryGrid');
        const cards = window.FrontierDirectoryGlobe?.visibleCards?.() || [];
        if (!grid || !cards.length) return;
        if (artworkPage !== cards[0]) { artworkPage = cards[0]; artworkCursor = 0; }
        const start = artworkCursor;
        for (let inspected = 0; inspected < cards.length && artworkRunning < ARTWORK_CONCURRENCY; inspected++) {
            const index = (start + inspected) % cards.length;
            const card = cards[index];
            const job = artworkJobs.get(card);
            if (!job || job.running || job.done || job.retryAt > Date.now() || (job.tracks && !job.cover && job.next >= job.tracks.length) || !grid.contains(card)) continue;
            const box = card.getBoundingClientRect();
            if (box.bottom < 0 || box.top > window.innerHeight + 200) continue;
            // Bounded workers select visible cards afresh after each phase.
            // A slow album cannot own the entire page's request queue.
            job.running = true;
            artworkRunning++;
            artworkCursor = (index + 1) % cards.length;
            void (async () => {
                try {
                    if (!job.tracks) {
                        const artwork = await lookupArtwork(job.url);
                        if (String(artwork.cover || '').startsWith('/api/v1/media/cover?')) {
                            job.cover = artwork.cover;
                        }
                        job.tracks = Array.isArray(artwork.metadata_tracks) ? artwork.metadata_tracks.slice(0, 8) : [];
                    }
                    if (job.cover) {
                        if (grid.contains(card) && window.FrontierDirectoryGlobe.visibleCards().includes(card)) { job.show(job.cover); job.done = true; }
                        return;
                    }
                    if (!grid.contains(card) || !window.FrontierDirectoryGlobe.visibleCards().includes(card)) return;
                    if (job.next >= job.tracks.length) { job.done = true; return; }
                    if (!window.FrontierMusicMetadata) { job.done = true; return; }
                    const tags = await window.FrontierMusicMetadata.metadata(job.tracks[job.next]);
                    job.next++;
                    if (tags.cover) job.cover = tags.cover;
                    if (job.cover && grid.contains(card) && window.FrontierDirectoryGlobe.visibleCards().includes(card)) { job.show(job.cover); job.done = true; }
                } catch (_) { job.retryAt = Date.now() + 2000; /* Retry on a later visibility event without a tight failure loop. */ }
                finally {
                    job.running = false;
                    artworkRunning--;
                    hydrateVisibleArtwork();
                }
            })();
        }
    }
    window.addEventListener('frontier-directory-visible', hydrateVisibleArtwork);
    window.addEventListener('scroll', hydrateVisibleArtwork, {passive: true});
    window.addEventListener('pagehide', event => { if (!event.persisted) artworkCache.clear(); });

    function cacheRead(key) {
        if (!key) return null;
        try {
            const raw = sessionStorage.getItem(CACHE_PREFIX + key);
            if (!raw || raw.length > MAX_CACHE_BYTES) return null;
            const value = JSON.parse(raw);
            return Array.isArray(value?.entries) ? value : null;
        } catch (_) {
            return null;
        }
    }

    function cacheWrite(key, value) {
        if (!key || !Array.isArray(value?.entries)) return;
        try {
            const raw = JSON.stringify(value);
            if (raw.length <= MAX_CACHE_BYTES) sessionStorage.setItem(CACHE_PREFIX + key, raw);
        } catch (_) {
            // Storage can be unavailable in privacy modes; network rendering still works.
        }
    }

    async function fetchCatalog(url, key, render) {
        if (!url) return;
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {'Accept': 'application/json'},
            cache: 'no-cache',
        });
        if (!response.ok) throw new Error(`目录数据加载失败：${response.status}`);
        const value = await response.json();
        if (!Array.isArray(value?.entries)) throw new Error('目录数据格式无效');
        cacheWrite(key, value);
        if (render) render(value.entries);
        return value;
    }

    function middleEllipsis(value, maximum = 44) {
        const characters = Array.from(String(value));
        if (characters.length <= maximum) return characters.join('');
        const head = Math.ceil((maximum - 3) / 2);
        const tail = Math.floor((maximum - 3) / 2);
        return `${characters.slice(0, head).join('')}...${characters.slice(-tail).join('')}`;
    }

    function renderCategories(entries) {
        const grid = document.getElementById('categoryGrid');
        if (!grid) return;
        const signature = JSON.stringify(entries);
        if (renderedCategories && signature !== renderedCategories) artworkCache.clear();
        renderedCategories = signature;
        grid.replaceChildren();
        if (!entries.length) {
            const empty = document.createElement('div');
            empty.className = 'empty';
            empty.textContent = window.frontierCloudCatalogConfig?.emptyText || '暂无分类目录';
            grid.append(empty);
            initDirectoryGlobe();
            return;
        }
        const fragment = document.createDocumentFragment();
        for (const entry of entries) {
            const card = document.createElement('a');
            card.className = 'card';
            card.href = String(entry.url || '#');
            card.title = String(entry.name || '');
            const artwork = document.createElement('div');
            artwork.className = 'card-artwork';
            artwork.ariaHidden = 'true';
            const placeholder = document.createElement('div');
            placeholder.className = 'card-placeholder';
            placeholder.textContent = Array.from(String(entry.name || '音'))[0];
            artwork.append(placeholder);
            const show = source => {
                artwork.querySelector('img')?.remove();
                const image = document.createElement('img');
                image.src = source;
                image.alt = '';
                image.decoding = 'async';
                image.draggable = false;
                placeholder.hidden = true;
                image.addEventListener('error', () => { image.hidden = true; placeholder.hidden = false; }, {once: true});
                artwork.append(image);
            };
            if (String(entry.cover || '').startsWith('/api/v1/media/cover?')) {
                show(entry.cover);
            }
            if (String(entry.artwork_url || '').startsWith('/api/v1/media/catalog/artwork?')) artworkJobs.set(card, {url: entry.artwork_url, tracks: null, show, next: 0, done: false});
            const title = document.createElement('div');
            title.className = 'card-title';
            title.textContent = middleEllipsis(entry.name || '');
            const arrow = document.createElement('div');
            arrow.className = 'card-arrow';
            arrow.textContent = '→';
            card.append(artwork, title, arrow);
            fragment.append(card);
        }
        grid.append(fragment);
        initDirectoryGlobe();
    }

    function initDirectoryGlobe() {
        window.FrontierDirectoryGlobe?.refresh();
    }

    function idle(callback) {
        if ('requestIdleCallback' in window) {
            window.requestIdleCallback(callback, {timeout: 1200});
        } else {
            setTimeout(callback, 250);
        }
    }

    function prefetchUrl(url, key = '') {
        if (!url) return;
        void fetchCatalog(url, key, null).catch(() => {});
    }

    window.FrontierCatalogCache = {
        read: cacheRead,
        write: cacheWrite,
        fetch: (url, key = '') => fetchCatalog(url, key, null),
    };

    function hiddenRevealStorageKey(mediaType) {
        return `frontier:hidden-reveal:${mediaType}`;
    }

    function includeHiddenRequested(url = new URL(window.location.href)) {
        const value = String(url.searchParams.get('include_hidden') || '').toLowerCase();
        return value === '1' || value === 'true';
    }

    function syncHiddenRevealState() {
        const mediaType = window.frontierCloudCatalogRevealKind;
        if (!['music', 'video'].includes(mediaType)) return true;
        const key = hiddenRevealStorageKey(mediaType);
        const url = new URL(window.location.href);
        if (includeHiddenRequested(url)) {
            try { sessionStorage.setItem(key, '1'); } catch (_) {}
            return true;
        }
        try {
            if (sessionStorage.getItem(key) === '1') {
                url.searchParams.set('include_hidden', 'true');
                window.location.replace(url.toString());
                return false;
            }
        } catch (_) {
            // Hidden reveal is a convenience state only; normal catalog browsing still works.
        }
        return true;
    }

    function bindHiddenRevealGesture() {
        const logo = document.getElementById('pageBrandLogo');
        const mediaType = window.frontierCloudCatalogRevealKind;
        if (!logo || !['music', 'video'].includes(mediaType)) return;

        let count = 0;
        let startedAt = 0;
        logo.addEventListener('click', () => {
            const now = Date.now();
            if (!startedAt || now - startedAt > HIDDEN_REVEAL_WINDOW_MS) {
                count = 0;
                startedAt = now;
            }
            count += 1;
            if (count < HIDDEN_REVEAL_CLICK_LIMIT) return;

            count = 0;
            startedAt = 0;
            try { sessionStorage.setItem(hiddenRevealStorageKey(mediaType), '1'); } catch (_) {}
            const url = new URL(window.location.href);
            url.searchParams.set('include_hidden', 'true');
            window.location.replace(url.toString());
        });
    }

    function bindPrefetch() {
        const links = Array.from(document.querySelectorAll('[data-catalog-prefetch]'));
        for (const link of links) {
            let started = false;
            const run = () => {
                if (started) return;
                started = true;
                prefetchUrl(link.dataset.catalogPrefetch, link.dataset.catalogCacheKey || '');
            };
            link.addEventListener('pointerenter', run, {once: true, passive: true});
            link.addEventListener('touchstart', run, {once: true, passive: true});
            link.addEventListener('focus', run, {once: true, passive: true});
            idle(run);
        }
        idle(() => {
            for (const url of window.frontierCloudPrefetchAssets || []) {
                fetch(url, {credentials: 'same-origin', cache: 'force-cache'}).catch(() => {});
            }
        });
    }

    function startCatalogPage() {
        const config = window.frontierCloudCatalogConfig;
        if (!config) return;
        const heading = document.querySelector('h1');
        if (heading && !document.getElementById('globeStage')) {
            heading.title = heading.textContent;
            heading.textContent = middleEllipsis(heading.textContent, 52);
        }
        if (Array.isArray(config.bootstrap)) {
            renderCategories(config.bootstrap);
            cacheWrite(config.cacheKey, {entries: config.bootstrap});
        } else {
            const cached = cacheRead(config.cacheKey);
            if (cached) renderCategories(cached.entries);
        }
        fetchCatalog(config.url, config.cacheKey, renderCategories).catch((error) => {
            const grid = document.getElementById('categoryGrid');
            if (grid && !grid.children.length) {
                const empty = document.createElement('div');
                empty.className = 'empty';
                empty.textContent = error.message;
                grid.append(empty);
            }
        });
    }

    let pageStarted = false;
    function startBrowserPage() {
        if (pageStarted) return;
        pageStarted = true;
        if (!syncHiddenRevealState()) return;
        startCatalogPage();
        bindHiddenRevealGesture();
        bindPrefetch();
    }

    // Category templates load this script after their complete catalog DOM.
    // Render immediately: an unrelated later script must not hold the directory
    // behind DOMContentLoaded while its download is stalled on a weak network.
    if (document.readyState !== 'loading' || document.getElementById('categoryGrid')) {
        startBrowserPage();
    } else {
        window.addEventListener('DOMContentLoaded', startBrowserPage, {once: true});
    }
})();
