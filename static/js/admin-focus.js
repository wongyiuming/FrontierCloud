(() => {
    const MODULE_ORDER = [
        'media', 'priority', 'lyrics', 'users',
        'security', 'network',
        'nodes', 'site', 'release', 'key', 'brand',
    ];
    const MODULE_RANK = new Map(MODULE_ORDER.map((name, index) => [name, index]));
    const consoleRoot = document.querySelector('.admin-console');

    function moduleChildren() {
        if (!consoleRoot) return [];
        return [...consoleRoot.children].filter(node => node.classList?.contains('admin-module'));
    }

    function reorderModules() {
        const modules = moduleChildren();
        if (!modules.length) return;
        const sortedModules = [...modules].sort((left, right) => {
            const leftRank = MODULE_RANK.get(left.dataset.adminModule) ?? Number.MAX_SAFE_INTEGER;
            const rightRank = MODULE_RANK.get(right.dataset.adminModule) ?? Number.MAX_SAFE_INTEGER;
            return leftRank - rightRank;
        });
        if (modules.every((module, index) => module === sortedModules[index])) return;
        for (const module of sortedModules) consoleRoot.append(module);
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

    const expansionObserver = new MutationObserver(records => {
        for (const record of records) {
            if (record.type !== 'attributes' || record.attributeName !== 'class') continue;
            const module = record.target;
            if (module.classList?.contains('admin-module') && module.classList.contains('expanded')) {
                focusExpanded(module);
            }
        }
    });

    function watchModule(module, reset = false) {
        if (reset) module.classList.remove('expanded');
        syncHeading(module, module.classList.contains('expanded'));
        expansionObserver.observe(module, {attributes: true, attributeFilter: ['class']});
    }

    // First normalize the static modules and any runtime modules that completed
    // synchronously before this script loaded.
    reorderModules();
    for (const module of moduleChildren()) watchModule(module, true);

    document.addEventListener('click', event => {
        const heading = event.target.closest?.('.module-heading');
        if (!heading) return;
        const module = heading.closest('.admin-module');
        if (!module) return;
        setTimeout(() => focusExpanded(module), 0);
    });

    // Site/release/brand panels may finish asynchronous initialization after
    // admin-focus.js starts. Observe only direct Admin module insertion. DOM
    // moves caused by reorderModules() are harmless because the reorder is
    // idempotent and performs no append when the order is already correct.
    if (consoleRoot) {
        let reorderScheduled = false;
        const scheduleReorder = () => {
            if (reorderScheduled) return;
            reorderScheduled = true;
            Promise.resolve().then(() => {
                reorderScheduled = false;
                reorderModules();
            });
        };
        const moduleObserver = new MutationObserver(records => {
            let needsReorder = false;
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (!(node instanceof HTMLElement) || !node.classList.contains('admin-module')) continue;
                    watchModule(node);
                    needsReorder = true;
                }
            }
            if (needsReorder) scheduleReorder();
        });
        moduleObserver.observe(consoleRoot, {childList: true});
    }
})();
