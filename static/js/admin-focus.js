(() => {
    function focusExpanded(module) {
        if (!module?.classList.contains('expanded')) return;
        requestAnimationFrame(() => {
            module.scrollIntoView({behavior: 'smooth', block: 'start', inline: 'nearest'});
        });
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
                for (const module of modules) observer.observe(module, {attributes: true, attributeFilter: ['class']});
            }
        }
    }).observe(document.body, {childList: true, subtree: true});
})();
