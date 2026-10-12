(() => {
    const panel = document.getElementById('priorityPanel');
    const list = document.getElementById('priorityList');
    const status = document.getElementById('prioritySaveState');
    const hint = document.getElementById('priorityDragHint');
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let orders = {}, drag = null, saving = false;
    const animations = new Map();
    const rows = kind => [...list.querySelectorAll(`[data-priority-kind="${kind}"]`)];
    const announce = (text, failed = false) => {
        status.textContent = text;
        status.classList.toggle('failed', failed);
    };

    function animateReorder(action, kind) {
        const elements = rows(kind);
        const before = new Map(elements.map(row => [row, row.getBoundingClientRect().top]));
        for (const row of elements) { animations.get(row)?.cancel(); animations.delete(row); }
        action();
        if (reduced.matches) return;
        const after = new Map(elements.map(row => [row, row.getBoundingClientRect().top]));
        for (const row of elements) {
            const distance = before.get(row) - after.get(row);
            if (Math.abs(distance) < 1) continue;
            const animation = row.animate([{transform: `translateY(${distance}px)`}, {transform: 'translateY(0)'}], {
                duration: 180, easing: 'cubic-bezier(.2,.8,.2,1)',
            });
            animations.set(row, animation);
            animation.finished.then(() => { if (animations.get(row) === animation) animations.delete(row); }).catch(() => {});
        }
    }

    function position(row, at, kind) {
        const peers = rows(kind).filter(peer => peer !== row);
        animateReorder(() => {
            if (at < peers.length) list.insertBefore(row, peers[at]);
            else if (peers.length) peers[peers.length - 1].after(row);
        }, kind);
    }

    function restore(original, kind) {
        animateReorder(() => {
            const current = rows(kind);
            const marker = document.createComment('priority-position');
            if (!current.length) return;
            list.insertBefore(marker, current[0]);
            for (const row of original) marker.before(row);
            marker.remove();
        }, kind);
    }

    function finishPointer(cancel = false) {
        if (!drag) return;
        const current = drag;
        drag = null;
        cancelAnimationFrame(current.frame);
        current.ghost.remove();
        current.row.classList.remove('priority-dragging');
        current.handle.setAttribute('aria-pressed', 'false');
        if (list.hasPointerCapture(current.pointer)) list.releasePointerCapture(current.pointer);
        document.body.classList.remove('priority-drag-active');
        if (cancel) {
            restore(current.original, current.kind);
            announce('已取消拖动');
        } else {
            save(current.row, current.kind, current.original).catch(error => announce(error.message, true));
        }
    }

    async function save(row, kind, original) {
        if (saving || !orders[kind]) return;
        const visible = rows(kind);
        if (visible.every((peer, index) => peer === original[index])) { announce('顺序未改变'); return; }
        const state = orders[kind];
        const name = row.dataset.priorityPath;
        const full = state.items.filter(item => item.path !== name);
        const at = visible.indexOf(row);
        let target;
        if (at + 1 < visible.length) target = full.findIndex(item => item.path === visible[at + 1].dataset.priorityPath);
        else target = full.findIndex(item => item.path === visible[at - 1]?.dataset.priorityPath) + 1;
        if (target < 0 || !state.items.some(item => item.path === name)) {
            restore(original, kind); announce('排序已变化，请刷新后重试', true); return;
        }
        const before = full[target]?.path || '';
        const after = full[target - 1]?.path || '';
        saving = true;
        panel.setAttribute('aria-busy', 'true');
        const controls = [...panel.querySelectorAll('button, input, select')].map(control => [control, control.disabled]);
        for (const [control] of controls) control.disabled = true;
        announce('正在保存顺序…');
        let success = false;
        try {
            const result = await api('/api/v1/media/admin/priority-order', {
                method: 'POST', headers: requestHeaders(),
                body: JSON.stringify({scope: state.scope, kind, revision: state.revision, path: name, before, after}),
            });
            success = true;
            orders[kind] = result;
            await loadMediaPriority(true);
            announce(`已保存顺序 · ${Number(result.changed)} 项优先级更新`);
        } catch (error) {
            restore(original, kind);
            announce(success ? `顺序已保存，列表刷新失败：${error.message}。请刷新。` : `${error.message}；已恢复显示顺序，请刷新确认。`, true);
        } finally {
            for (const [control, disabled] of controls) if (control.isConnected) control.disabled = disabled;
            panel.removeAttribute('aria-busy');
            saving = false;
            list.querySelector(`[data-priority-path="${CSS.escape(name)}"] .priority-drag-handle`)?.focus({preventScroll: true});
        }
    }

    // Pointer events only update coordinates. Layout reads and DOM changes are
    // batched once per animation frame; the scroll speed is time based.
    function frame(time) {
        if (!drag) return;
        const d = drag;
        const bounds = list.getBoundingClientRect();
        const elapsed = Math.min(32, time - (d.lastTime || time));
        d.lastTime = time;
        const edge = 44;
        const speed = d.y < bounds.top + edge ? -Math.min(1, (bounds.top + edge - d.y) / edge)
            : d.y > bounds.bottom - edge ? Math.min(1, (d.y - bounds.bottom + edge) / edge) : 0;
        const oldScroll = list.scrollTop;
        if (speed) list.scrollTop += speed * elapsed * .55;
        if (d.updated || oldScroll !== list.scrollTop) {
            d.updated = false;
            d.ghost.style.transform = `translate3d(0,${d.y - d.startY}px,0)`;
            const peers = rows(d.kind).filter(peer => peer !== d.row);
            let at = peers.length;
            // offsetTop excludes in-flight FLIP transforms from hit testing.
            for (let i = 0; i < peers.length; i++) {
                const midpoint = bounds.top + peers[i].offsetTop - list.scrollTop + peers[i].offsetHeight / 2;
                if (d.y < midpoint) { at = i; break; }
            }
            const previous = rows(d.kind).indexOf(d.row);
            if (at !== previous) {
                position(d.row, at, d.kind);
                announce(`移动到第 ${at + 1} 位，松开保存`);
            }
        }
        d.frame = requestAnimationFrame(frame);
    }

    function decorate(row, kind) {
        row.classList.add('priority-reorderable');
        const handle = document.createElement('button');
        handle.type = 'button'; handle.className = 'priority-drag-handle';
        handle.textContent = '⠿';
        const name = row.dataset.priorityPath.split('/').pop();
        handle.setAttribute('aria-label', `拖动调整 ${name} 的顺序`);
        handle.setAttribute('aria-describedby', 'priorityDragHint');
        handle.setAttribute('aria-pressed', 'false');
        handle.title = '拖动排序；键盘 ↑ ↓ 调整一位；Esc 取消';
        const begin = event => {
            if (event.button !== 0 || saving || drag || priorityLoading || panel.querySelector('input[type="range"]:disabled')) return;
            event.preventDefault(); handle.focus({preventScroll: true});
            const rect = row.getBoundingClientRect();
            const ghost = row.cloneNode(true);
            ghost.classList.add('priority-drag-ghost'); ghost.setAttribute('aria-hidden', 'true'); ghost.inert = true;
            Object.assign(ghost.style, {position: 'fixed', left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`});
            document.body.append(ghost);
            drag = {row, handle, kind, ghost, pointer: event.pointerId, original: rows(kind), startY: event.clientY, y: event.clientY, updated: false};
            row.classList.add('priority-dragging'); handle.setAttribute('aria-pressed', 'true');
            document.body.classList.add('priority-drag-active');
            // The list stays attached while rows move. Capturing a row handle
            // would lose capture when DOM insertion repositions its ancestor.
            list.setPointerCapture(event.pointerId);
            drag.frame = requestAnimationFrame(frame);
            announce(`正在移动 ${name}，Esc 取消`);
        };
        handle.addEventListener('pointerdown', begin);
        handle.addEventListener('keydown', event => {
            if (event.key === 'Escape') { event.preventDefault(); finishPointer(true); return; }
            if (!['ArrowUp', 'ArrowDown'].includes(event.key) || saving || drag || priorityLoading || panel.querySelector('input[type="range"]:disabled')) return;
            event.preventDefault();
            const original = rows(kind), at = original.indexOf(row), target = at + (event.key === 'ArrowUp' ? -1 : 1);
            if (target < 0 || target >= original.length) { announce('已到当前页边界，可切换分页继续调整'); return; }
            position(row, target, kind);
            save(row, kind, original).catch(error => announce(error.message, true));
        });
        row.prepend(handle);
    }

    window.FrontierPriorityDrag = {
        cancel: () => finishPointer(true),
        activate(data, nextOrders) {
            finishPointer(true);
            orders = nextOrders;
            for (const kind of ['media', 'directory']) {
                if (!orders[kind] || rows(kind).length < 2) continue;
                for (const row of rows(kind)) decorate(row, kind);
            }
            hint.textContent = data.searching ? '搜索时可设置数值；进入对应目录后可拖动排序。'
                : '优先级越大越靠前 · 拖动把手自动保存 · 键盘 ↑ ↓ 调整，Esc 取消拖动。';
            if (data.pagination?.pages > 1) hint.textContent += ' 当前页拖动会同时校验未显示的同级项目。';
        },
    };
    list.addEventListener('pointermove', event => { if (drag?.pointer === event.pointerId) { drag.y = event.clientY; drag.updated = true; } });
    list.addEventListener('pointerup', event => {
        if (drag?.pointer !== event.pointerId) return;
        // Flush the last position before consuming the target anchors.
        drag.y = event.clientY; drag.updated = true;
        cancelAnimationFrame(drag.frame); frame(performance.now()); finishPointer();
    });
    list.addEventListener('pointercancel', () => finishPointer(true));
    list.addEventListener('lostpointercapture', () => { if (drag) finishPointer(true); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && drag) { event.preventDefault(); finishPointer(true); } });
    document.addEventListener('visibilitychange', () => { if (document.hidden) finishPointer(true); });
    window.addEventListener('resize', () => finishPointer(true));
})();
