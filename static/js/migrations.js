/* Independent Admin migration controls; resource authority stays in the API. */
(() => {
    const byId = id => document.getElementById(id), panel = byId('migrationsPanel');
    if (!panel) return;
    const base = '/api/v1/media/admin/migrations';
    let preview = null, taskId = '', historyAfter = '', memberAfter = '', unitCursor = '', timer = null, loading = false, busy = false, viewVersion = 0;
    const names = new Map(), seenHistory = new Set();
    const states = {observing: '检查中', ready: '可执行', blocked: '未通过', admitting: '准备中', prepared: '验证连接',
        queued: '等待执行', pending: '待执行', copying: '传输中', verifying: '校验中', verified: '已校验', committing: '切换中',
        publishing: '发布中', published: '待切换', committed: '已切换', cleaned: '已清理', cleanup_pending: '等待源端清理',
        paused: '已暂停', cancelling: '取消中', admission_aborting: '取消准备', cancelled: '已取消', failed: '需核查', completed: '已完成'};
    const messages = {migration_admin_master_required: '需要主节点', migration_planner_busy: '另一个计划正在检查',
        migration_plan_expired_or_restarted: '计划已过期，请重新检查',
        migration_admin_maintenance_fenced: '维护中', migration_transfer_direct_connect_failed: '节点无法直连',
        migration_peer_tls_verification_failed: '节点证书验证失败', migration_admission_busy: '准备尚未结束，请稍后重试',
        migration_waiting_low_traffic: '等待低峰', migration_waiting_reader: '等待播放或下载结束', migration_waiting_writer: '等待写入结束'};
    const size = value => {
        const bytes = Math.max(0, Number(value || 0));
        if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(2)} GiB`;
        if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MiB`;
        if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
        return `${bytes} B`;
    };
    function make(tag, text = '', className = '') {
        const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
    }
    function button(text, action) {
        const node = make('button', text); node.type = 'button'; node.disabled = busy;
        node.onclick = () => work(action); return node;
    }
    const note = text => { byId('migrationOperationStatus').textContent = text || ''; };
    const name = id => names.get(id) || String(id).slice(0, 12);
    async function request(path, value) {
        const options = value === undefined ? {} : {method: 'POST', headers: requestHeaders(), body: JSON.stringify(value)};
        const response = await fetch(base + path, activityRequestOptions(options));
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) { location.href = '/api/v1/media'; throw new Error('登录已失效'); }
        if (!response.ok) {
            const error = new Error(messages[data.code] || '操作未完成，请刷新核查'); error.code = data.code; throw error;
        }
        return data;
    }
    async function work(action) {
        if (busy) return;
        busy = true; viewVersion += 1; note('处理中');
        for (const node of panel.querySelectorAll('button, select')) node.disabled = true;
        try { await action(); note(''); }
        catch (error) { note(error.message); }
        finally {
            busy = false;
            for (const node of panel.querySelectorAll('button, select')) node.disabled = false;
            schedule();
        }
    }
    function renderPreview() {
        const root = byId('migrationPreview'); root.replaceChildren();
        if (!preview) return;
        const card = make('div', '', 'migration-card');
        card.append(make('strong', `计划 · ${states[preview.state] || '状态未知'}`),
            make('p', `${preview.object_count} 个文件 · ${preview.unit_count} 个目录 · ${size(preview.total_bytes)}`));
        if (preview.state === 'ready') card.append(button('执行迁移', async () => {
            const sourceId = preview.report.source_id, targetId = preview.report.target_id;
            if (!confirm(`迁移 ${name(sourceId)} → ${name(targetId)}，共 ${size(preview.total_bytes)}？`)) return;
            const row = await request(`/plans/${preview.plan_id}/start`, {approved_sha256: preview.review_sha256,
                confirm_source_id: sourceId, confirm_target_id: targetId});
            taskId = row.task_id; unitCursor = ''; preview = null; renderPreview(); renderTask(row); await history(true);
        }));
        if (preview.state === 'blocked') card.append(make('p', `发现 ${Number(preview.report.blocker_count || 0)} 项冲突，未开始迁移。`));
        root.append(card);
    }
    function renderTask(row) {
        const root = byId('migrationTask'); root.replaceChildren();
        const card = make('div', '', 'migration-card'); root.append(card);
        const state = row.state === 'paused' ? (row.pause_reason === 'manual' ? '手动暂停' : '避峰暂停') : states[row.state] || '状态未知';
        card.append(make('strong', `${name(row.source_id)} → ${name(row.target_id)} · ${state}`));
        const progress = document.createElement('progress'); progress.max = Math.max(1, row.total_bytes); progress.value = row.committed_bytes;
        progress.setAttribute('aria-label', '目录切换进度'); card.append(progress, make('p', `已切换 ${size(row.committed_bytes)} / ${size(row.total_bytes)}`));
        const current = row.current_object;
        if (current && ['copying', 'verifying'].includes(row.state)) {
            const percent = current.expected_bytes ? current.received_bytes / current.expected_bytes * 100 : 0;
            card.append(make('p', `${current.physical_path} · ${percent.toFixed(1)}% · ${size(current.received_bytes)} / ${size(current.expected_bytes)}`, 'migration-path'));
        }
        if (row.error_code) card.append(make('p', messages[row.error_code] || '存在冲突，资源仍受保护。'));
        const controls = make('div', '', 'nodes-toolbar');
        const action = verb => async () => {
            if (verb === 'cancel' && !confirm('取消迁移？已切换的目录不回退。')) return;
            const current = await request(`/tasks/${row.task_id}`);
            const updated = await request(`/tasks/${row.task_id}`, {revision: current.revision, action: verb});
            unitCursor = ''; renderTask(updated);
        };
        if (['queued', 'copying', 'verifying', 'committing', 'cleanup_pending'].includes(row.state)) controls.append(button('暂停', action('pause')));
        if (row.state === 'paused') controls.append(button('恢复', action('resume')));
        if (row.state === 'failed' || row.error_code && ['admitting', 'prepared', 'admission_aborting', 'cancelling'].includes(row.state)) controls.append(button('重试', action('retry')));
        if (['admitting', 'prepared', 'queued', 'copying', 'verifying', 'committing', 'cleanup_pending', 'paused', 'failed'].includes(row.state)) controls.append(button('取消', action('cancel')));
        card.append(controls);
        for (const unit of row.units) card.append(make('p', `${unit.parent_path} · ${states[unit.state] || '状态未知'} · ${size(unit.total_bytes)}`, 'migration-path'));
        if (unitCursor) card.append(button('首批目录', async () => { unitCursor = ''; renderTask(await request(`/tasks/${row.task_id}`)); }));
        if (row.next_cursor) card.append(button('更多目录', async () => {
            unitCursor = row.next_cursor; renderTask(await request(`/tasks/${row.task_id}?after=${encodeURIComponent(unitCursor)}`));
        }));
    }
    async function history(reset = false, version = viewVersion) {
        const data = await request(`/tasks${!reset && historyAfter ? `?after=${encodeURIComponent(historyAfter)}` : ''}`);
        if (version !== viewVersion) return;
        if (reset) { historyAfter = ''; seenHistory.clear(); byId('migrationHistory').replaceChildren(); }
        for (const row of data.items) {
            if (seenHistory.has(row.task_id)) continue;
            seenHistory.add(row.task_id);
            byId('migrationHistory').append(button(`${new Date(row.created_at * 1000).toLocaleString()} · ${states[row.state] || '状态未知'}`, async () => {
                taskId = row.task_id; unitCursor = ''; renderTask(await request(`/tasks/${taskId}`));
            }));
        }
        historyAfter = data.next_cursor; byId('migrationHistoryMore').hidden = !historyAfter;
    }
    async function refresh(members = false) {
        if (loading || busy) return;
        loading = true; const version = viewVersion;
        try {
            if (members) {
                const data = await request('/members'); if (version !== viewVersion) return;
                for (const [id, label] of [['migrationSource', '源节点'], ['migrationTarget', '目标节点']]) {
                    const select = byId(id), previous = select.value; select.replaceChildren(new Option(label, ''));
                    for (const row of data.items) {
                        names.set(row.member_id, `${row.peer_endpoint} · ${row.transport}`);
                        const option = new Option(`${name(row.member_id)} · ${row.status === 'online' ? '在线' : '离线'}`, row.member_id);
                        option.disabled = row.status !== 'online' || !row.storage_enabled; select.append(option);
                    }
                    select.value = previous;
                }
                memberAfter = data.next_cursor || ''; byId('migrationMembersMore').hidden = !memberAfter;
                await history(true, version);
            }
            if (version !== viewVersion) return;
            if (preview) { const data = await request(`/plans/${preview.plan_id}`); if (version !== viewVersion) return; preview = data; renderPreview(); }
            if (taskId) {
                const row = await request(`/tasks/${taskId}${unitCursor ? `?after=${encodeURIComponent(unitCursor)}` : ''}`);
                if (version === viewVersion) renderTask(row);
            }
        } catch (error) {
            if (version === viewVersion) {
                if (error.code === 'migration_plan_expired_or_restarted') { preview = null; renderPreview(); }
                note(error.message);
            }
        }
        finally { loading = false; schedule(); }
    }
    function schedule() {
        clearTimeout(timer);
        if (panel.classList.contains('expanded') && !document.hidden) timer = setTimeout(() => refresh(), 3000);
    }
    byId('migrationPlanForm').onsubmit = event => {
        event.preventDefault();
        work(async () => {
            const sourceId = byId('migrationSource').value, targetId = byId('migrationTarget').value;
            if (!sourceId || !targetId || sourceId === targetId) throw new Error('请选择两个不同的节点');
            preview = await request('/plans', {request_id: crypto.randomUUID().replaceAll('-', ''), source_id: sourceId, target_id: targetId});
            renderPreview();
        });
    };
    byId('migrationsRefresh').onclick = () => refresh(true);
    byId('migrationMembersMore').onclick = () => work(async () => {
        const data = await request(`/members?after=${encodeURIComponent(memberAfter)}`);
        for (const row of data.items) {
            names.set(row.member_id, `${row.peer_endpoint} · ${row.transport}`);
            for (const id of ['migrationSource', 'migrationTarget']) {
                const select = byId(id);
                if ([...select.options].some(option => option.value === row.member_id)) continue;
                const option = new Option(`${name(row.member_id)} · ${row.status === 'online' ? '在线' : '离线'}`, row.member_id);
                option.disabled = row.status !== 'online' || !row.storage_enabled; select.append(option);
            }
        }
        memberAfter = data.next_cursor || ''; byId('migrationMembersMore').hidden = !memberAfter;
    });
    byId('migrationHistoryMore').onclick = () => work(() => history());
    new MutationObserver(() => {
        if (panel.classList.contains('expanded')) refresh(true); else clearTimeout(timer);
    }).observe(panel, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && panel.classList.contains('expanded')) refresh(); else clearTimeout(timer);
    });
    window.addEventListener('pagehide', () => clearTimeout(timer));
})();
