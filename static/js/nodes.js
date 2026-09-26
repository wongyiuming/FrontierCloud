/* Admin node operations: observable state, effective-state confirmation and safe controls. */
(() => {
    const $ = id => document.getElementById(id);
    const panel = $('nodesPanel');
    if (!panel) return;

    if (!document.querySelector('link[data-node-observability]')) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = '/static/css/nodes-observability.css?v=20260926';
        link.dataset.nodeObservability = '1';
        document.head.append(link);
    }

    let refreshQueue = Promise.resolve();
    let refreshTimer = null;
    const setStatus = text => { $('nodeOperationStatus').textContent = text || ''; };
    const visible = (id, show) => $(id).classList.toggle('hidden', !show);
    const post = (path, value = {}) => api(`/api/v1/media/admin/nodes${path}`, {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(value),
    });
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const gib = value => `${(Number(value || 0) / 1073741824).toFixed(2)} GiB`;
    const mib = value => `${(Number(value || 0) / 1048576).toFixed(0)} MiB`;
    const shortId = value => value ? String(value).slice(0, 12) : '-';
    const pct = (part, total) => total > 0
        ? Math.max(0, Math.min(100, Math.round(Number(part || 0) * 100 / Number(total)))) : 0;
    const localTime = stamp => stamp ? new Date(Number(stamp) * 1000).toLocaleString() : '从未';

    function relativeTime(stamp) {
        if (!stamp) return '从未';
        const delta = Math.round(Number(stamp) - Date.now() / 1000);
        const future = delta > 0;
        const seconds = Math.abs(delta);
        let value;
        if (seconds < 60) value = `${seconds} 秒`;
        else if (seconds < 3600) value = `${Math.floor(seconds / 60)} 分钟`;
        else if (seconds < 86400) value = `${Math.floor(seconds / 3600)} 小时`;
        else value = `${Math.floor(seconds / 86400)} 天`;
        return future ? `${value}后` : `${value}前`;
    }

    function duration(seconds) {
        const value = Math.max(0, Number(seconds || 0));
        if (value < 60) return `${Math.round(value)} 秒`;
        if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
        if (value < 86400) return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分`;
        return `${Math.floor(value / 86400)} 天 ${Math.floor((value % 86400) / 3600)} 小时`;
    }

    function make(tag, className = '', text = '') {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== '') node.textContent = text;
        return node;
    }

    function pill(text, tone = 'muted') {
        return make('span', `node-pill ${tone}`, text);
    }

    function syncLine(value) {
        const labels = {
            effective: ['已生效', 'effective'],
            syncing: ['同步中', 'syncing'],
            awaiting: ['等待确认', 'syncing'],
            offline: ['节点离线', 'offline'],
        };
        const [label, tone] = labels[value] || ['未知', ''];
        const row = make('div', `node-sync-line ${tone}`);
        row.append(make('i', 'node-sync-dot'), make('span', '', label));
        return row;
    }

    function kv(items) {
        const grid = make('div', 'node-kv');
        for (const [label, value] of items) {
            grid.append(make('span', '', label), make('span', '', String(value)));
        }
        return grid;
    }

    function progress(part, total) {
        const shell = make('div', 'node-progress');
        const bar = make('i');
        bar.style.width = `${pct(part, total)}%`;
        shell.append(bar);
        return shell;
    }

    async function action(work, defaultMessage = '已完成') {
        try {
            // Keep this exact value: federation browser acceptance waits while it is present.
            setStatus('处理中');
            const result = await work();
            await refresh();
            setStatus(typeof result === 'string' ? result : defaultMessage);
        } catch (error) {
            setStatus(error.message || String(error));
        }
    }

    function button(label, work, className = '', title = '') {
        const result = make('button', className, label);
        result.type = 'button';
        if (title) result.title = title;
        result.onclick = () => action(work);
        return result;
    }

    function ensureOverview() {
        let overview = $('nodeFleetOverview');
        if (overview) return overview;
        overview = make('div', 'node-fleet-overview');
        overview.id = 'nodeFleetOverview';
        panel.querySelector('.nodes-table-scroll').before(overview);
        return overview;
    }

    async function loadData() {
        const node = await api('/api/v1/media/admin/nodes');
        let observability = {members: [], cluster: {}, relationships: []};
        try {
            observability = await api('/api/v1/media/admin/nodes/observability');
        } catch (error) {
            console.warn('node observability unavailable', error);
        }
        return {node, observability};
    }

    function overviewCard(label, value, detail, tone) {
        const card = make('div', `node-overview-stat ${tone || ''}`);
        card.append(make('small', '', label), make('strong', '', value), make('span', '', detail));
        return card;
    }

    function renderOverview(node, observability) {
        const overview = ensureOverview();
        overview.replaceChildren();
        if (node.role !== 'Master') {
            const online = (node.relationships || []).filter(item => item.status === 'online').length;
            overview.append(
                overviewCard('当前角色', node.role, '资源策略由 Master 控制', 'good'),
                overviewCard('上游关系', String((node.relationships || []).length), `${online} 条在线`, 'storage'),
                overviewCard('协议版本', `v${node.protocol}`, node.app_version, 'compute'),
                overviewCard('控制面', '被管理节点', '展示实际状态；资源策略只由 Master 下发', 'backup'),
            );
            return;
        }
        const pool = node.storage_pool || {};
        const members = observability.members || [];
        const followers = members.filter(item => item.member_kind === 'Follower');
        const online = followers.filter(item => item.connection?.status === 'online').length;
        const slots = members.reduce((sum, item) => sum + Number(item.compute?.worker_slots || 0), 0);
        const running = Number(observability.cluster?.running || 0);
        const backupEnabled = followers.filter(item => item.backup?.enabled).length;
        const backupHealthy = followers.filter(item => item.backup?.health === 'healthy').length;
        overview.append(
            overviewCard('Follower', `${online} / ${followers.length}`,
                online === followers.length ? '全部在线' : '存在离线或降级节点', 'good'),
            overviewCard('Storage', `${gib(pool.used_bytes)} / ${gib(pool.allocated_bytes)}`,
                `在线可写 ${gib(pool.online_writable_bytes)}`, 'storage'),
            overviewCard('Compute', `${running} / ${slots}`,
                `运行中 / 并发上限 · 24h 完成 ${observability.cluster?.completed_24h || 0}`, 'compute'),
            overviewCard('Backup', `${backupHealthy} / ${backupEnabled}`,
                backupEnabled ? '健康 / 已启用备份节点' : '尚未启用备份节点', 'backup'),
        );
    }

    function connectionPanel(relation, observed) {
        const connection = observed?.connection || {};
        const section = make('section', 'node-resource-panel connection');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', '连接状态'));
        const state = connection.status || relation?.status || 'unknown';
        heading.append(pill(state === 'online' ? 'Online' : state,
            state === 'online' ? 'good' : (state === 'degraded' ? 'warn' : (state === 'local' ? 'info' : 'bad'))));
        const current = Number(connection.current_rtt_ms ?? relation?.rtt_ms ?? 0);
        section.append(heading, make('div', 'node-metric-primary', state === 'local' ? 'Local' : `${current} ms`));
        if (state !== 'local') {
            section.append(kv([
                ['最近心跳', relativeTime(connection.last_heartbeat || relation?.last_heartbeat)],
                ['最近时间', localTime(connection.last_heartbeat || relation?.last_heartbeat)],
                ['1h 平均', `${connection.avg_rtt_ms || current} ms`],
                ['1h 最低 / 最高', `${connection.min_rtt_ms || current} / ${connection.max_rtt_ms || current} ms`],
                ['RTT 样本', `${connection.sample_count || (current ? 1 : 0)} 次`],
                ['连续失败', connection.failures ?? relation?.failures ?? 0],
                ['历史恢复', connection.recoveries ?? relation?.recoveries ?? 0],
            ]));
            section.append(make('div', 'node-note', 'RTT 是 Master 到该节点 HTTPS 控制面的往返时间；滚动统计最近 1 小时成功样本。'));
        } else {
            section.append(make('div', 'node-note', 'Master Local 是本机资源，不经过节点间 HTTPS 心跳。'));
        }
        return section;
    }

    function storagePanel(member, observed) {
        const section = make('section', 'node-resource-panel storage');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', 'Storage'));
        heading.append(pill(member.storage_enabled ? 'Enabled' : 'Disabled', member.storage_enabled ? 'info' : 'muted'));
        const used = Number(member.used_bytes || 0), allocated = Number(member.allocated_bytes || 0);
        section.append(heading, make('div', 'node-metric-primary', `${gib(used)} / ${gib(allocated)}`), progress(used, allocated));
        section.append(kv([
            ['使用率', `${pct(used, allocated)}%`],
            ['当前可写', gib(member.online_writable_bytes)],
            ['已预留', gib(member.reserved_bytes)],
            ['物理剩余', gib(member.physical_free_bytes)],
            ['传输', member.transport || 'Local'],
        ]));
        section.append(syncLine(observed?.sync?.storage || (member.member_kind === 'MasterLocal' ? 'effective' : 'awaiting')));
        section.append(make('div', 'node-note', '配额是 Master 允许该成员承载的业务容量；实际写入还受物理剩余空间约束。'));
        return section;
    }

    function jobRow(job) {
        const row = make('div', 'node-job');
        row.append(make('code', '', String(job.job_type || '-').toUpperCase()));
        const body = make('div');
        body.append(make('strong', '', `${shortId(job.job_id)} · ${job.state || 'unknown'}`),
            document.createElement('br'), document.createTextNode(job.placement_reason || '调度原因未知'));
        row.append(body, make('small', '', `${job.attempts || 0} 次 · ${relativeTime(job.updated_at)}`));
        return row;
    }

    function computePanel(member, observed) {
        const compute = observed?.compute || member.compute || {};
        const section = make('section', 'node-resource-panel compute');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', 'Compute'));
        heading.append(pill(compute.enabled ? 'Enabled' : 'Disabled', compute.enabled ? 'info' : 'muted'));
        const running = Number(compute.running || 0), slots = Number(compute.worker_slots || 0);
        const primary = make('div', 'node-metric-primary');
        primary.append(document.createTextNode(`${running} / ${slots}`), make('small', '', '运行中 / 并发上限'));
        section.append(heading, primary, progress(running, Math.max(slots, 1)));
        section.append(kv([
            ['可用槽位', compute.available_slots ?? Math.max(0, slots - running)],
            ['定向排队', compute.queued_pinned || 0],
            ['共享可领取', compute.shared_queued || 0],
            ['24h 完成', compute.completed_24h || 0],
            ['24h 重试', compute.retries_24h || 0],
            ['CPU', `${compute.cpu_percent || 0}%`],
            ['可用内存', mib(compute.memory_available_bytes)],
        ]));
        section.append(syncLine(observed?.sync?.compute || (member.member_kind === 'MasterLocal' ? 'effective' : 'awaiting')));
        const details = make('details', 'node-details');
        details.append(make('summary', '', `最近任务与调度依据 · capability: ${(compute.capabilities || []).join(', ') || '-'}`));
        const jobs = make('div', 'node-job-list');
        const recent = compute.recent || [];
        if (recent.length) recent.slice(0, 8).forEach(job => jobs.append(jobRow(job)));
        else jobs.append(make('div', 'node-note', '暂无该节点 Worker 任务记录。'));
        details.append(jobs);
        section.append(details, make('div', 'node-note', 'Slots 是真实最大并发 Worker 数；运行数来自 Master 当前有效 lease。'));
        return section;
    }

    function backupTone(health) {
        if (health === 'healthy') return 'good';
        if (health === 'running' || health === 'waiting-first-backup') return 'warn';
        if (health === 'stale') return 'bad';
        return 'muted';
    }

    function backupLabel(backup) {
        return ({healthy: 'Healthy', running: 'Backing up', stale: 'Stale',
            'waiting-first-backup': 'Waiting first backup', disabled: 'Disabled'})[backup.health]
            || backup.raw_state || 'Unknown';
    }

    function backupPanel(member, observed) {
        const backup = observed?.backup || member.backup || {};
        const section = make('section', 'node-resource-panel backup');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', 'Backup'), pill(backupLabel(backup), backupTone(backup.health)));
        section.append(heading, make('div', 'node-metric-primary',
            backup.last_success ? relativeTime(backup.last_success) : (backup.enabled ? '尚未成功' : '未启用')));
        section.append(kv([
            ['最近成功', localTime(backup.last_success)],
            ['距今', backup.last_success ? duration(backup.lag_seconds) : '-'],
            ['最近大小', backup.last_size_bytes ? mib(backup.last_size_bytes) : '-'],
            ['恢复点', `${backup.recovery_points || 0} 个`],
            ['最近尝试', backup.last_attempt ? `${localTime(backup.last_attempt)} · ${backup.last_attempt_state || '-'}` : '-'],
            ['下次计划', backup.next_due ? `${localTime(backup.next_due)} · ${relativeTime(backup.next_due)}` : '-'],
            ['最近校验', backup.checksum ? `SHA256 ${shortId(backup.checksum)}…` : '-'],
        ]));
        section.append(syncLine(observed?.sync?.backup || (member.member_kind === 'MasterLocal' ? 'effective' : 'awaiting')));
        const details = make('details', 'node-details');
        details.append(make('summary', '', '技术详情'), kv([
            ['内部状态', backup.raw_state || '-'],
            ['Generation', backup.generation ? String(backup.generation) : '-'],
            ['调度周期', '24 小时'],
            ['失败重试', '5 分钟'],
        ]));
        section.append(details, make('div', 'node-note', '主状态以最近成功恢复点为准；Generation 仅作为内部代际标识。'));
        return section;
    }

    function resourceConfig(member) {
        const grid = make('div', 'node-config-grid');

        const storageBox = make('div', 'node-config-box');
        const storageLabel = make('label');
        const storageEnabled = document.createElement('input');
        storageEnabled.type = 'checkbox'; storageEnabled.checked = Boolean(member.storage_enabled);
        storageLabel.append(storageEnabled, document.createTextNode(' Storage 承载业务数据'));
        const storageInputs = make('div', 'node-config-inputs');
        const capacity = document.createElement('input');
        capacity.type = 'number'; capacity.min = '1'; capacity.max = '10240';
        capacity.value = String(Math.max(1, Math.ceil(Number(member.allocated_bytes || 0) / 1073741824)));
        capacity.disabled = !storageEnabled.checked;
        storageEnabled.onchange = () => { capacity.disabled = !storageEnabled.checked; };
        storageInputs.append(capacity, make('span', '', 'GiB 配额'));
        storageBox.append(storageLabel, make('small', '', '关闭后停止新写入；已有数据不会自动迁走。'), storageInputs);

        const computeBox = make('div', 'node-config-box');
        const computeLabel = make('label');
        const computeEnabled = document.createElement('input');
        computeEnabled.type = 'checkbox'; computeEnabled.checked = Boolean(member.compute?.enabled);
        computeLabel.append(computeEnabled, document.createTextNode(' Compute Worker'));
        const computeInputs = make('div', 'node-config-inputs');
        const slots = document.createElement('input');
        slots.type = 'number'; slots.min = '0'; slots.max = '256'; slots.value = String(member.compute?.worker_slots || 0);
        slots.disabled = !computeEnabled.checked;
        computeEnabled.onchange = () => { slots.disabled = !computeEnabled.checked; };
        computeInputs.append(slots, make('span', '', '并发 slots'));
        computeBox.append(computeLabel, make('small', '', '最多同时执行多少 hash / probe / metadata 任务。'), computeInputs);

        const backupBox = make('div', 'node-config-box');
        const backupLabel = make('label');
        const backupEnabled = document.createElement('input');
        backupEnabled.type = 'checkbox'; backupEnabled.checked = Boolean(member.backup?.enabled);
        backupLabel.append(backupEnabled, document.createTextNode(' Backup 恢复点'));
        backupBox.append(backupLabel, make('small', '', 'Master 每 24h 发送业务恢复包；失败后约 5 分钟重试。'));

        grid.append(storageBox, computeBox, backupBox);
        return {grid, storageEnabled, capacity, computeEnabled, slots, backupEnabled};
    }

    async function waitForEffective(memberId, timeoutMs = 75000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            await sleep(2200);
            const data = await api('/api/v1/media/admin/nodes/observability');
            const member = (data.members || []).find(item => item.member_id === memberId);
            if (!member) continue;
            const sync = member.sync || {};
            if (sync.storage === 'effective' && sync.compute === 'effective' && sync.backup === 'effective')
                return `配置已在 Follower 生效 · ${new Date().toLocaleTimeString()}`;
            if (member.connection?.status === 'offline')
                return 'Master 已保存配置，但 Follower 当前离线，尚未生效';
        }
        return 'Master 已保存配置；Follower 尚未在心跳中确认，请检查连接状态';
    }

    function controls(member, relation) {
        const shell = make('div', 'node-card-controls');
        if (!relation || relation.state !== 'active') {
            if (member.member_kind === 'MasterLocal') {
                shell.append(make('div', 'node-note', 'Master Local 是本机固定资源，仅允许调整 Storage 配额。'));
                const actions = make('div', 'node-card-actions');
                const input = document.createElement('input');
                input.type = 'number'; input.min = '1'; input.max = '10240';
                input.value = String(Math.max(1, Math.ceil(Number(member.allocated_bytes || 0) / 1073741824)));
                input.style.width = '90px';
                actions.append(input, button('更新本机配额', () => post(`/${member.member_id}/resources`, {
                    storage_enabled: true, storage_capacity_gib: Number(input.value),
                    compute_enabled: true, worker_slots: 1, backup_enabled: false,
                }), 'apply'));
                shell.append(actions);
            }
            return shell;
        }

        const config = resourceConfig(member);
        const actions = make('div', 'node-card-actions');
        const mode = document.createElement('select');
        mode.setAttribute('aria-label', `${member.member_id} 数据传输模式`);
        mode.title = 'Relay 由 Master 中转业务数据；Direct 允许数据面直接访问 Follower。';
        for (const value of ['Relay', 'Direct']) {
            const option = document.createElement('option'); option.value = value; option.textContent = value; mode.append(option);
        }
        mode.value = relation.mode;
        mode.onchange = () => action(async () => {
            await post(`/${relation.relationship_id}/mode`, {mode: mode.value});
            return `传输模式已保存为 ${mode.value}`;
        });
        const apply = button('应用资源配置', async () => {
            await post(`/${relation.relationship_id}/resources`, {
                storage_enabled: config.storageEnabled.checked,
                storage_capacity_gib: config.storageEnabled.checked ? Number(config.capacity.value) : 0,
                compute_enabled: config.computeEnabled.checked,
                worker_slots: config.computeEnabled.checked ? Number(config.slots.value) : 0,
                backup_enabled: config.backupEnabled.checked,
            });
            setStatus('Master 已保存，等待 Follower 心跳确认实际生效…');
            return await waitForEffective(member.member_id);
        }, 'apply', '只有 Follower 回报的 Observed 配置与 Desired 一致才显示已生效。');
        const revoke = button('撤销关系', () => post(`/${relation.relationship_id}/revoke`), 'danger',
            'Follower 仍持有有效 Storage Pool 文件时后端会拒绝撤销。');
        actions.append(mode, apply, revoke);
        shell.append(config.grid, actions);
        return shell;
    }

    function memberCard(member, relation, observed) {
        const row = document.createElement('tr'); row.className = 'node-card-row';
        const cell = document.createElement('td'); cell.colSpan = 5;
        const card = make('article', 'node-card');
        const header = make('header', 'node-card-header');
        const title = make('div', 'node-card-title');
        title.append(make('strong', '', member.member_kind === 'MasterLocal' ? 'Master Local' : member.member_id),
            make('small', '', relation?.peer_endpoint || '本机资源'));
        const badges = make('div', 'node-card-badges');
        if (member.member_kind === 'MasterLocal') badges.append(pill('LOCAL', 'info'));
        else badges.append(pill(relation?.status === 'online' ? 'ONLINE' : String(relation?.status || 'UNKNOWN').toUpperCase(),
            relation?.status === 'online' ? 'good' : 'bad'));
        badges.append(pill(member.transport || relation?.mode || 'Local', 'muted'));
        header.append(title, badges);
        const grid = make('div', 'node-card-grid');
        grid.append(connectionPanel(relation, observed), storagePanel(member, observed),
            computePanel(member, observed), backupPanel(member, observed));
        card.append(header, grid, controls(member, relation));
        cell.append(card); row.append(cell);
        return row;
    }

    function followerRelationCard(relation, observed) {
        const row = document.createElement('tr'); row.className = 'node-card-row';
        const cell = document.createElement('td'); cell.colSpan = 5;
        const card = make('article', 'node-card');
        const header = make('header', 'node-card-header');
        const title = make('div', 'node-card-title');
        title.append(make('strong', '', relation.peer_id), make('small', '', relation.peer_endpoint || ''));
        const badges = make('div', 'node-card-badges');
        badges.append(pill(relation.status === 'online' ? 'ONLINE' : String(relation.status || 'UNKNOWN').toUpperCase(),
            relation.status === 'online' ? 'good' : 'bad'), pill(relation.mode || '-', 'muted'));
        header.append(title, badges);
        const grid = make('div', 'node-card-grid');
        grid.style.gridTemplateColumns = 'minmax(260px,.8fr) minmax(0,2fr)';
        const managed = make('section', 'node-resource-panel storage');
        managed.append(make('div', 'node-resource-heading', ''),
            make('div', 'node-metric-primary', 'Master Managed'),
            make('div', 'node-note', 'Storage / Compute / Backup 的 Desired 配置由 Master 下发，本节点只回报 Observed 状态。'));
        grid.append(connectionPanel(relation, observed), managed);
        const controlsBox = make('div', 'node-card-controls');
        controlsBox.append(make('div', 'node-note', 'Follower 不能自行修改资源策略。'));
        const actions = make('div', 'node-card-actions');
        actions.append(button('撤销关系', () => post(`/${relation.relationship_id}/revoke`), 'danger'));
        controlsBox.append(actions);
        card.append(header, grid, controlsBox); cell.append(card); row.append(cell);
        return row;
    }

    function renderPoolSummary(node) {
        const pool = node.storage_pool;
        $('storagePoolSummary').textContent = pool
            ? `Storage Pool · 已分配 ${gib(pool.allocated_bytes)} · 已使用 ${gib(pool.used_bytes)} · 已预留 ${gib(pool.reserved_bytes)} · 在线可写 ${gib(pool.online_writable_bytes)} · 离线持有 ${gib(pool.offline_stored_bytes)}`
            : (node.role === 'Follower'
                ? 'Follower · Desired 资源配置由 Master 管理；本页展示本节点和上游关系的实际状态。'
                : 'Standalone · 当前仅使用本地资源。');
    }

    function renderRows(node, observability) {
        const body = $('nodeRelationships'); body.replaceChildren();
        const relations = new Map((node.relationships || []).map(item => [item.peer_id, item]));
        const observed = new Map((observability.members || []).map(item => [item.member_id, item]));
        if (node.storage_pool) {
            for (const member of node.storage_pool.members || [])
                body.append(memberCard(member, relations.get(member.member_id), observed.get(member.member_id)));
            return;
        }
        const observedRelations = new Map((observability.relationships || []).map(item => [item.relationship_id, item]));
        for (const relation of node.relationships || [])
            body.append(followerRelationCard(relation, observedRelations.get(relation.relationship_id)));
    }

    async function refresh() {
        const current = refreshQueue.then(async () => {
            const {node, observability} = await loadData();
            $('nodeIdentity').textContent = `${node.role} · ${node.node_id} · ${node.app_version} / v${node.protocol}${node.endpoint ? ' · ' + node.endpoint : ''}`;
            visible('nodePromotion', node.role === 'Standalone');
            visible('nodePairing', node.role !== 'Standalone');
            visible('nodeIssuePair', node.role === 'Follower');
            visible('nodeImportPair', node.role === 'Master');
            visible('nodeReinitialize', node.role !== 'Standalone');
            $('nodePairPackage').readOnly = node.role === 'Follower';
            const role = $('nodeRole'), capacity = $('masterLocalCapacity');
            capacity.disabled = role.value !== 'Master';
            role.onchange = () => { capacity.disabled = role.value !== 'Master'; };
            renderOverview(node, observability);
            renderPoolSummary(node);
            renderRows(node, observability);
            return {node, observability};
        });
        refreshQueue = current.catch(() => {});
        return current;
    }

    function startAutoRefresh() {
        if (refreshTimer) return;
        refreshTimer = setInterval(() => {
            if (panel.classList.contains('expanded')) refresh().catch(error => setStatus(error.message));
        }, 10000);
    }
    function stopAutoRefresh() {
        if (refreshTimer) clearInterval(refreshTimer);
        refreshTimer = null;
    }

    $('nodesRefresh').onclick = () => action(async () => {}, '已刷新');
    panel.querySelector('.module-heading').addEventListener('click', () => {
        if (panel.classList.contains('expanded')) {
            refresh().catch(error => setStatus(error.message));
            startAutoRefresh();
        } else stopAutoRefresh();
    });
    $('nodePromotion').onsubmit = event => {
        event.preventDefault();
        const role = $('nodeRole').value;
        action(() => post('/promote', {
            role, endpoint: $('nodeEndpoint').value,
            local_capacity_gib: role === 'Master' ? Number($('masterLocalCapacity').value) : null,
        }));
    };
    $('nodeIssuePair').onclick = () => action(async () => {
        $('nodePairPackage').value = JSON.stringify(await post('/pair-package'), null, 2);
    });
    $('nodeImportPair').onclick = () => action(() => post('/pair', {
        package: JSON.parse($('nodePairPackage').value),
    }));
    $('nodeReinitialize').onsubmit = event => {
        event.preventDefault();
        action(() => post('/reinitialize', {confirmation: $('nodeResetConfirmation').value}));
    };
})();
