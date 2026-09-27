(() => {
    const current = document.getElementById('systemReleaseCurrent');
    const target = document.getElementById('systemReleaseTarget');
    if (!current || !target || current.dataset.semanticVersionUi === '1') return;

    current.dataset.semanticVersionUi = '1';
    current.hidden = true;
    target.hidden = true;

    const host = current.parentElement;
    const stat = host?.closest('.release-stat');
    const label = stat?.querySelector('small');
    if (label) label.textContent = '生产版本';

    const summary = document.createElement('span');
    summary.id = 'systemReleaseVersionSummary';
    host?.appendChild(summary);

    const render = () => {
        const currentSha = current.textContent.trim();
        const targetSha = target.textContent.trim();
        if (currentSha && currentSha !== '-' && targetSha && targetSha !== '-') {
            summary.textContent = currentSha === targetSha
                ? `${currentSha} · 已与 main HEAD 一致`
                : `当前 ${currentSha} → 待发布 ${targetSha}`;
            return;
        }
        summary.textContent = currentSha && currentSha !== '-'
            ? `当前 ${currentSha}`
            : targetSha && targetSha !== '-'
                ? `待发布 ${targetSha}`
                : '尚未加载';
    };

    const observer = new MutationObserver(render);
    observer.observe(current, {childList: true, characterData: true, subtree: true});
    observer.observe(target, {childList: true, characterData: true, subtree: true});
    render();
})();
