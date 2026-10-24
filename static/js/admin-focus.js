(() => {
    const MODULE_ORDER = [
        'media', 'priority', 'lyrics', 'users',
        'security', 'network',
        'nodes', 'migrations', 'site', 'key', 'brand',
    ];
    const MODULE_RANK = new Map(MODULE_ORDER.map((name, index) => [name, index]));
    const consoleRoot = document.querySelector('.admin-console');
    const navigation = document.getElementById('adminModuleLinks');
    const navigationPanel = document.getElementById('adminNavigation');
    const navigationToggle = document.getElementById('adminNavigationToggle');
    const navigationButtons = new Map();

    function updateNavigation(modules) {
        if (!navigation) return;
        for (const module of modules) {
            const name = module.dataset.adminModule;
            if (!navigationButtons.has(name)) {
                const button = document.createElement('button');
                button.type = 'button';
                button.dataset.moduleTarget = name;
                button.textContent = module.querySelector('.module-heading strong')?.textContent || name;
                button.onclick = () => {
                    if (!module.classList.contains('expanded')) module.querySelector('.module-heading')?.click();
                    focusExpanded(module);
                    navigationPanel?.classList.remove('open');
                    navigationToggle?.setAttribute('aria-expanded', 'false');
                    module.querySelector('.module-heading')?.focus({preventScroll: true});
                };
                navigationButtons.set(name, button);
            }
        }
        const buttons = modules.map(module => navigationButtons.get(module.dataset.adminModule));
        if (!buttons.every((button, index) => navigation.children[index] === button)) navigation.append(...buttons);
    }

    navigationToggle?.addEventListener('click', () => {
        const open = !navigationPanel.classList.contains('open');
        navigationPanel.classList.toggle('open', open);
        navigationToggle.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape' || !navigationPanel?.classList.contains('open')) return;
        navigationPanel.classList.remove('open');
        navigationToggle.setAttribute('aria-expanded', 'false');
        navigationToggle.focus();
    });
    document.addEventListener('pointerdown', event => {
        if (!navigationPanel?.classList.contains('open') || navigationPanel.contains(event.target) || navigationToggle.contains(event.target)) return;
        navigationPanel.classList.remove('open');
        navigationToggle.setAttribute('aria-expanded', 'false');
    });

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
        updateNavigation(sortedModules);
        if (modules.every((module, index) => module === sortedModules[index])) return;
        for (const module of sortedModules) consoleRoot.append(module);
    }

    function syncHeading(module, expanded) {
        const heading = module.querySelector?.('.module-heading');
        if (!heading) return;
        heading.setAttribute('aria-expanded', String(expanded));
        const indicator = heading.querySelector('b');
        if (indicator) indicator.textContent = expanded ? '−' : '＋';
        const button = navigationButtons.get(module.dataset.adminModule);
        if (expanded) button?.setAttribute('aria-current', 'true');
        else button?.removeAttribute('aria-current');
    }

    let focusRevision = 0;
    const cancelPendingFocus = () => { focusRevision++; };
    consoleRoot?.addEventListener('wheel', cancelPendingFocus, {passive: true});
    consoleRoot?.addEventListener('touchmove', cancelPendingFocus, {passive: true});
    document.addEventListener('pointerdown', cancelPendingFocus, {capture: true, passive: true});
    document.addEventListener('keydown', event => {
        if (['Escape', 'PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown', ' '].includes(event.key)) cancelPendingFocus();
    });

    function focusExpanded(module) {
        if (!module?.classList.contains('expanded')) return;
        const revision = ++focusRevision;
        requestAnimationFrame(async () => {
            if (revision !== focusRevision) return;
            // A collapsing sibling above the target keeps moving its start
            // position during the flex transition. Align only to settled layout.
            const transitions = moduleChildren().flatMap(node => node.getAnimations?.() || [])
                .filter(animation => ['flex-basis', 'flex-grow'].includes(animation.transitionProperty));
            await Promise.allSettled(transitions.map(animation => animation.finished));
            requestAnimationFrame(() => {
                if (revision !== focusRevision || !module.isConnected || !module.classList.contains('expanded')) return;
                module.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start', inline: 'nearest'});
            });
        });
    }

    const expansionObserver = new MutationObserver(records => {
        for (const record of records) {
            if (record.type !== 'attributes' || record.attributeName !== 'class') continue;
            const module = record.target;
            syncHeading(module, module.classList.contains('expanded'));
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

    // Site/brand panels may finish asynchronous initialization after
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
