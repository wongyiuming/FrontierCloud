(() => {
    'use strict';

    const CACHE_PREFIX = 'frontier:catalog:v1:';
    const MAX_CACHE_BYTES = 256 * 1024;
    const HIDDEN_REVEAL_CLICK_LIMIT = 15;
    const HIDDEN_REVEAL_WINDOW_MS = 60 * 1000;

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
        grid.replaceChildren();
        if (!entries.length) {
            const empty = document.createElement('div');
            empty.className = 'empty';
            empty.textContent = window.frontierCloudCatalogConfig?.emptyText || '暂无分类目录';
            grid.append(empty);
            return;
        }
        const fragment = document.createDocumentFragment();
        for (const entry of entries) {
            const card = document.createElement('a');
            card.className = 'card';
            card.href = String(entry.url || '#');
            card.title = String(entry.name || '');
            const title = document.createElement('div');
            title.className = 'card-title';
            title.textContent = `📁 ${middleEllipsis(entry.name || '')}`;
            const arrow = document.createElement('div');
            arrow.className = 'card-arrow';
            arrow.textContent = '→';
            card.append(title, arrow);
            fragment.append(card);
        }
        grid.append(fragment);
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
        if (heading) {
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

    window.addEventListener('DOMContentLoaded', () => {
        if (!syncHiddenRevealState()) return;
        startCatalogPage();
        bindHiddenRevealGesture();
        bindPrefetch();
    });
})();
