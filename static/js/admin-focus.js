(() => {
    const MODULE_ORDER = [
        'media', 'priority', 'lyrics', 'users',
        'security', 'network',
        'nodes', 'site', 'release', 'key',
    ];
    const MODULE_RANK = new Map(MODULE_ORDER.map((name, index) => [name, index]));

    function reorderModules() {
        const consoleRoot = document.querySelector('.admin-console');
        if (!consoleRoot) return;
        const modules = [...consoleRoot.children].filter(node => node.classList?.contains('admin-module'));
        modules.sort((left, right) => {
            const leftRank = MODULE_RANK.get(left.dataset.adminModule) ?? Number.MAX_SAFE_INTEGER;
            const rightRank = MODULE_RANK.get(right.dataset.adminModule) ?? Number.MAX_SAFE_INTEGER;
            return leftRank - rightRank;
        });
        for (const module of modules) consoleRoot.append(module);
    }

    function syncHeading(module, expanded) {
        const heading = module.querySelector?.('.module-heading');
        if (!heading) return;
        heading.setAttribute('aria-expanded', String(expanded));
        const indicator = heading.querySelector('b');
        if (indicator) indicator.textContent = expanded ? '−' : '＋';
    }

    function focusExpanded(module) {
        if (!module?.classList.contains('expanded')) return;
        requestAnimationFrame(() => {
            module.scrollIntoView({behavior: 'smooth', block: 'start', inline: 'nearest'});
        });
    }

    // release-admin.js and maintenance-admin.js run before this script, so both
    // runtime-created system modules are present before the canonical reorder.
    reorderModules();

    for (const module of document.querySelectorAll('.admin-module')) {
        module.classList.remove('expanded');
        syncHeading(module, false);
    }

    document.addEventListener('click', event => {
        const heading = event.target.closest?.('.module-heading');
        if (!heading) return;
        const module = heading.closest('.admin-module');
        if (!module) return;
        setTimeout(() => focusExpanded(module), 0);
    });

    const observer = new MutationObserver(records => {
        for (const record of records) {
            if (record.type !== 'attributes' || record.attributeName !== 'class') continue;
            const module = record.target;
            if (module.classList?.contains('admin-module') && module.classList.contains('expanded')) {
                focusExpanded(module);
            }
        }
    });

    for (const module of document.querySelectorAll('.admin-module')) {
        observer.observe(module, {attributes: true, attributeFilter: ['class']});
    }

    new MutationObserver(records => {
        for (const record of records) {
            for (const node of record.addedNodes) {
                if (!(node instanceof HTMLElement)) continue;
                const modules = node.matches?.('.admin-module') ? [node] : [...node.querySelectorAll?.('.admin-module') || []];
                for (const module of modules) {
                    syncHeading(module, module.classList.contains('expanded'));
                    observer.observe(module, {attributes: true, attributeFilter: ['class']});
                }
            }
        }
    }).observe(document.body, {childList: true, subtree: true});
})();
