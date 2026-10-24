(() => {
    'use strict';
    const stage = document.getElementById('globeStage'), grid = document.getElementById('categoryGrid');
    const toggle = document.getElementById('globeToggle');
    if (!stage || !grid || !toggle) return;
    const controls = document.getElementById('albumPagination'), dots = document.getElementById('albumPages');
    let cards = [], list = false, page = 0, drag = null, moved = false, wheelTime = -1000;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const pages = () => Math.ceil(cards.length / 9);
    function update() {
        page = Math.max(0, Math.min(pages() - 1, Math.round(grid.scrollLeft / Math.max(1, grid.clientWidth))));
        Array.from(dots?.children || []).forEach((dot, index) => dot.setAttribute('aria-current', String(index === page)));
        const status = document.getElementById('albumPageStatus');
        if (status) status.textContent = cards.length ? `${page + 1} / ${pages()}` : '';
        window.dispatchEvent(new Event('frontier-directory-visible'));
    }
    function go(index, smooth = true) {
        if (list) return;
        const target = Math.max(0, Math.min(pages() - 1, index));
        grid.scrollTo({left: target * grid.clientWidth, behavior: smooth && !reduced.matches ? 'smooth' : 'instant'});
    }
    function refresh() {
        cards = Array.from(grid.querySelectorAll('.card'));
        const count = document.getElementById('globeCount');
        if (count) count.textContent = `${cards.length} 个目录`;
        if (cards.length) {
            grid.replaceChildren();
            if (list) grid.append(...cards);
            else for (let i = 0; i < cards.length; i += 9) {
                const group = document.createElement('div'); group.className = 'album-page';
                group.setAttribute('role', 'group'); group.setAttribute('aria-label', `第 ${Math.floor(i / 9) + 1} 页`);
                group.append(...cards.slice(i, i + 9)); grid.append(group);
            }
        }
        dots?.replaceChildren();
        for (let i = 0; i < pages(); i++) {
            const dot = document.createElement('span'); dot.className = 'album-page-dot'; dots?.append(dot);
        }
        if (controls) controls.hidden = list || pages() <= 1;
        go(page, false); update();
    }
    toggle.addEventListener('click', () => {
        list = !list; document.documentElement.classList.toggle('frontier-covers', !list);
        stage.classList.toggle('orbit-list', list); toggle.textContent = list ? '前沿UI' : '列表视图';
        toggle.setAttribute('aria-pressed', String(list));
        stage.setAttribute('aria-label', list ? stage.dataset.listLabel || '音乐目录列表' : stage.dataset.gridLabel || '九宫格专辑目录，左右滑动翻页');
        const hint = document.getElementById('globeHint'); if (hint) hint.textContent = list ? hint.dataset.listLabel || '' : hint.dataset.pathLabel || hint.dataset.directoryLabel || '音乐目录';
        refresh();
    });
    grid.addEventListener('scroll', update, {passive: true});
    grid.addEventListener('pointerdown', event => {
        if (list || event.pointerType !== 'mouse' || event.button !== 0) return;
        drag = {id: event.pointerId, x: event.clientX, start: grid.scrollLeft}; moved = false;
    });
    grid.addEventListener('pointermove', event => {
        if (!drag || drag.id !== event.pointerId) return;
        const distance = event.clientX - drag.x; if (!moved && Math.abs(distance) < 8) return;
        moved = true; grid.setPointerCapture(event.pointerId); stage.classList.add('orbit-dragging'); grid.scrollLeft = drag.start - distance;
    });
    function release(event) {
        if (drag?.id !== event.pointerId) return;
        const startPage = Math.round(drag.start / Math.max(1, grid.clientWidth)), distance = event.clientX - drag.x;
        drag = null; stage.classList.remove('orbit-dragging');
        if (grid.hasPointerCapture(event.pointerId)) grid.releasePointerCapture(event.pointerId);
        if (moved) go(startPage + (Math.abs(distance) > grid.clientWidth * .12 ? (distance < 0 ? 1 : -1) : 0));
    }
    grid.addEventListener('pointerup', release);
    grid.addEventListener('pointercancel', event => { if (drag?.id === event.pointerId) { drag = null; moved = false; stage.classList.remove('orbit-dragging'); go(page); } });
    grid.addEventListener('click', event => { if (!list && moved && event.detail !== 0) { event.preventDefault(); event.stopPropagation(); moved = false; } }, true);
    grid.addEventListener('dragstart', event => { if (!list) event.preventDefault(); });
    stage.addEventListener('wheel', event => {
        if (list || pages() <= 1 || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
        // Wide square-cover pages may extend below the viewport. Preserve
        // vertical wheel scrolling so every row and the footer stay reachable.
        if (stage.getBoundingClientRect().height > window.innerHeight - 100) return;
        event.preventDefault(); if (event.timeStamp - wheelTime < 450 || Math.abs(event.deltaY) < 8) return;
        wheelTime = event.timeStamp; go(page + Math.sign(event.deltaY));
    }, {passive: false});
    stage.addEventListener('keydown', event => {
        if (list || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); go(event.key === 'Home' ? 0 : event.key === 'End' ? pages() - 1 : page + (event.key === 'ArrowRight' ? 1 : -1));
    });
    new ResizeObserver(() => { go(page, false); update(); }).observe(stage);
    window.FrontierDirectoryGlobe = {refresh, visibleCards: () => list ? cards : cards.slice(page * 9, page * 9 + 9)};
})();
