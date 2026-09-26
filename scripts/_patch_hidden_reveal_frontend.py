from pathlib import Path


def patch(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


patch(
    "static/js/media-browser.js",
    "    const MAX_CACHE_BYTES = 256 * 1024;\n",
    "    const MAX_CACHE_BYTES = 256 * 1024;\n    const HIDDEN_REVEAL_CLICK_LIMIT = 15;\n    const HIDDEN_REVEAL_WINDOW_MS = 60 * 1000;\n",
    "reveal constants",
)
patch(
    "static/js/media-browser.js",
    "        cacheWrite(key, value);\n        if (render) render(value.entries);\n",
    "        if (response.headers.get('X-Frontier-Hidden-Reveal') !== '1') cacheWrite(key, value);\n        if (render) render(value.entries);\n",
    "revealed cache bypass",
)
patch(
    "static/js/media-browser.js",
    '''    window.FrontierCatalogCache = {
        read: cacheRead,
        write: cacheWrite,
        fetch: (url, key = '') => fetchCatalog(url, key, null),
    };

''',
    '''    window.FrontierCatalogCache = {
        read: cacheRead,
        write: cacheWrite,
        fetch: (url, key = '') => fetchCatalog(url, key, null),
    };

    function clearCatalogSessionCache() {
        try {
            for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
                const key = sessionStorage.key(index);
                if (key?.startsWith(CACHE_PREFIX)) sessionStorage.removeItem(key);
            }
        } catch (_) {
            // Reload still fetches authoritative catalog data when storage is unavailable.
        }
    }

    function bindHiddenRevealGesture() {
        const logo = document.getElementById('pageBrandLogo');
        const mediaType = window.frontierCloudCatalogRevealKind;
        if (!logo || !['music', 'video'].includes(mediaType)) return;

        let count = 0;
        let startedAt = 0;
        let revealing = false;
        logo.addEventListener('click', async () => {
            if (revealing) return;
            const now = Date.now();
            if (!startedAt || now - startedAt > HIDDEN_REVEAL_WINDOW_MS) {
                count = 0;
                startedAt = now;
            }
            count += 1;
            if (count < HIDDEN_REVEAL_CLICK_LIMIT) return;
            count = 0;
            startedAt = 0;
            revealing = true;
            try {
                const response = await fetch(
                    `/api/v1/media/catalog/reveal?media_type=${encodeURIComponent(mediaType)}`,
                    {
                        method: 'POST',
                        credentials: 'same-origin',
                        cache: 'no-store',
                        headers: {'Accept': 'application/json', 'X-Frontier-Hidden-Reveal': '1'},
                    },
                );
                if (!response.ok) throw new Error(`隐藏资源解锁失败：${response.status}`);
                clearCatalogSessionCache();
                window.location.reload();
            } catch (error) {
                revealing = false;
                console.error(error);
            }
        });
    }

''',
    "reveal gesture",
)
patch(
    "static/js/media-browser.js",
    '''    window.addEventListener('DOMContentLoaded', () => {
        startCatalogPage();
        bindPrefetch();
    });
''',
    '''    window.addEventListener('DOMContentLoaded', () => {
        startCatalogPage();
        bindHiddenRevealGesture();
        bindPrefetch();
    });
''',
    "bind reveal",
)
patch(
    "static/media/category.html",
    '''        window.frontierCloudStunUrls = {{STUN_URLS_JSON}};
        window.frontierCloudWebrtcIntervalMs = {{WEBRTC_INTERVAL_MS}};
        window.frontierCloudCatalogConfig = {{CATALOG_CONFIG_JSON}};
''',
    '''        window.frontierCloudCatalogRevealKind =
            brandKind === 'music' ? 'music' : (brandKind === 'media' ? 'video' : '');
        window.frontierCloudStunUrls = {{STUN_URLS_JSON}};
        window.frontierCloudWebrtcIntervalMs = {{WEBRTC_INTERVAL_MS}};
        window.frontierCloudCatalogConfig = {{CATALOG_CONFIG_JSON}};
''',
    "category reveal type",
)
