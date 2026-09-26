from pathlib import Path


def patch(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


# Complete the local-node category path after the backend patch: hidden remains a
# presentation filter, and revealed links simply carry the public query flag.
patch(
    "app/api/v1/media.py",
    "def _get_media_categories_sync(media_type, valid_exts, hidden: set[str]):\n",
    "def _get_media_categories_sync(media_type, valid_exts, hidden: set[str], include_hidden=False):\n",
    "category sync signature",
)
patch(
    "app/api/v1/media.py",
    '            categories.append({"name": entry.name, "url": _category_url(media_type, rel_entry)})\n',
    '            categories.append({"name": entry.name, "url": _category_url(media_type, rel_entry, include_hidden=include_hidden)})\n',
    "category sync URL",
)
patch(
    "app/api/v1/media.py",
    '''    else:
        categories = await asyncio.to_thread(_get_media_categories_sync, media_type, valid_exts, hidden)
        if include_hidden:
            for entry in categories:
                entry["url"] = _category_url(media_type, entry["url"].split("path=", 1)[1], include_hidden=True)
''',
    '''    else:
        categories = await asyncio.to_thread(
            _get_media_categories_sync,
            media_type,
            valid_exts,
            hidden,
            include_hidden,
        )
''',
    "category sync call",
)

patch(
    "static/js/media-browser.js",
    "    const MAX_CACHE_BYTES = 256 * 1024;\n",
    "    const MAX_CACHE_BYTES = 256 * 1024;\n    const HIDDEN_REVEAL_CLICK_LIMIT = 15;\n    const HIDDEN_REVEAL_WINDOW_MS = 60 * 1000;\n",
    "reveal constants",
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

''',
    "hidden reveal helpers",
)
patch(
    "static/js/media-browser.js",
    '''    window.addEventListener('DOMContentLoaded', () => {
        startCatalogPage();
        bindPrefetch();
    });
''',
    '''    window.addEventListener('DOMContentLoaded', () => {
        if (!syncHiddenRevealState()) return;
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
