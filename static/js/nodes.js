/* Admin-only strict Master/Follower resource-pool controls. */
(() => {
    const element = id => document.getElementById(id);
    const panel = element('nodesPanel');
    if (!panel) return;
    let refreshQueue = Promise.resolve();
    const status = text => { element('nodeOperationStatus').textContent = text; };
    const visible = (id, show) => element(id).classList.toggle('hidden', !show);
    const post = (path, value = {}) => api(`/api/v1/media/admin/nodes${path}`, {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(value),
    });
    const gib = value => `${(Number(value || 0) / 1073741824).toFixed(2)} GiB`;
    async function action(work) {
        try { status('处理中'); await work(); await refresh(); status('已完成'); }
        catch (error) { status(error.message); }
    }
    function button(label, work) {
        const result = document.createElement('button'); result.type = 'button'; result.textContent = label;
        result.onclick = () => action(work); return result;
    }
    function cell(row, text) {
        const value = document.createElement('td'); value.textContent = text; value.style.whiteSpace = 'pre-line'; row.appendChild(value); return value;
    }
    function resourceControls(member, relation) {
        const box = document.createElement('div'); box.className = 'node-resource-controls';
        const storage = document.createElement('label');
        const storageEnabled = document.createElement('input'); storageEnabled.type = 'checkbox'; storageEnabled.checked = Boolean(member.storage_enabled);
        const capacity = document.createElement('input'); capacity.type = 'number'; capacity.min = '1'; capacity.max = '10240';
        capacity.value = String(Math.max(1, Math.ceil(Number(member.allocated_bytes || 0) / 1073741824)));
        capacity.disabled = !storageEnabled.checked; storageEnabled.onchange = () => { capacity.disabled = !storageEnabled.checked; };
        storage.append(storageEnabled, document.createTextNode(' Storage '), capacity, document.createTextNode(' GiB'));
        const compute = document.createElement('label');
        const computeEnabled = document.createElement('input'); computeEnabled.type = 'checkbox'; computeEnabled.checked = Boolean(member.compute?.enabled);
        const slots = document.createElement('input'); slots.type = 'number'; slots.min = '0'; slots.max = '256'; slots.value = String(member.compute?.worker_slots || 0);
        slots.disabled = !computeEnabled.checked; computeEnabled.onchange = () => { slots.disabled = !computeEnabled.checked; };
        compute.append(computeEnabled, document.createTextNode(' Compute '), slots, document.createTextNode(' slots'));
        const backup = document.createElement('label'); const backupEnabled = document.createElement('input');
        backupEnabled.type = 'checkbox'; backupEnabled.checked = Boolean(member.backup?.enabled);
        backup.append(backupEnabled, document.createTextNode(' Backup'));
        const save = button('保存三类资源', () => post(`/${relation.relationship_id}/resources`, {
            storage_enabled: storageEnabled.checked,
            storage_capacity_gib: storageEnabled.checked ? Number(capacity.value) : 0,
            compute_enabled: computeEnabled.checked,
            worker_slots: computeEnabled.checked ? Number(slots.value) : 0,
            backup_enabled: backupEnabled.checked,
        }));
        box.append(storage, compute, backup, save); return box;
    }
    function masterLocalControls(member) {
        const box = document.createElement('div'); box.className = 'node-resource-controls';
        const label = document.createElement('label');
        const capacity = document.createElement('input'); capacity.type = 'number'; capacity.min = '1'; capacity.max = '10240';
        capacity.value = String(Math.max(1, Math.ceil(Number(member.allocated_bytes || 0) / 1073741824)));
        label.append(document.createTextNode('Master Local '), capacity, document.createTextNode(' GiB'));
        box.append(label, button('更新固定额度', () => post(`/${member.member_id}/resources`, {
            storage_enabled: true, storage_capacity_gib: Number(capacity.value),
            compute_enabled: true, worker_slots: 1, backup_enabled: false,
        })));
        return box;
    }
    function refresh() {
        const current = refreshQueue.then(async () => {
            const node = await api('/api/v1/media/admin/nodes');
            element('nodeIdentity').textContent = `${node.role} · ${node.node_id} · ${node.app_version} / v${node.protocol}${node.endpoint ? ' · ' + node.endpoint : ''}`;
            visible('nodePromotion', node.role === 'Standalone'); visible('nodePairing', node.role !== 'Standalone');
            visible('nodeIssuePair', node.role === 'Follower'); visible('nodeImportPair', node.role === 'Master');
            visible('nodeReinitialize', node.role !== 'Standalone'); element('nodePairPackage').readOnly = node.role === 'Follower';
            const role = element('nodeRole'); const capacity = element('masterLocalCapacity');
            capacity.disabled = role.value !== 'Master'; role.onchange = () => { capacity.disabled = role.value !== 'Master'; };
            const pool = node.storage_pool;
            element('storagePoolSummary').textContent = pool
                ? `Allocated ${gib(pool.allocated_bytes)} · Used ${gib(pool.used_bytes)} · Reserved ${gib(pool.reserved_bytes)} · Available ${gib(pool.available_bytes)} · Online Writable ${gib(pool.online_writable_bytes)} · Offline Stored ${gib(pool.offline_stored_bytes)}`
                : (node.role === 'Follower' ? '资源状态由 Master 调度，本机不持有业务目录。' : 'Standalone 本地存储');
            const body = element('nodeRelationships'); body.replaceChildren();
            const relations = new Map((node.relationships || []).map(item => [item.peer_id, item]));
            const members = pool?.members || [];
            if (pool) {
                for (const member of members) {
                    const relation = relations.get(member.member_id);
                    const row = document.createElement('tr');
                    cell(row, `${member.member_kind === 'MasterLocal' ? 'Master Local' : member.member_id}\n${relation?.peer_endpoint || node.endpoint || ''}`);
                    const heartbeat = relation?.last_heartbeat ? new Date(relation.last_heartbeat * 1000).toLocaleString() : '-';
                    cell(row, `${member.health}${relation ? ` / ${relation.status}\n${relation.rtt_ms} ms · ${heartbeat}` : '\nLocal'}`);
                    cell(row, `${member.storage_enabled ? 'Enabled' : 'Disabled'} · ${member.transport}\nUsed ${gib(member.used_bytes)} / ${gib(member.allocated_bytes)}\nReserved ${gib(member.reserved_bytes)} · Writable ${gib(member.online_writable_bytes)}\nPhysical free ${gib(member.physical_free_bytes)}`);
                    cell(row, `${member.compute?.enabled ? 'Enabled' : 'Disabled'}\nSlots ${member.compute?.available_slots || 0} / ${member.compute?.worker_slots || 0}\nCPU ${member.compute?.cpu_percent || 0}%`);
                    const operations = cell(row, `${member.backup?.enabled ? member.backup?.state || 'pending' : 'Disabled'}\nGeneration ${member.backup?.generation || 0} · Lag ${member.backup?.lag_seconds || 0}s\n${relation?.mode || 'Local'}`);
                    if (relation && relation.state === 'active') {
                        const mode = document.createElement('select'); mode.setAttribute('aria-label', `${member.member_id} 数据传输模式`);
                        for (const value of ['Relay', 'Direct']) { const option = document.createElement('option'); option.value = value; option.textContent = value; mode.append(option); }
                        mode.value = relation.mode; mode.onchange = () => action(() => post(`/${relation.relationship_id}/mode`, {mode: mode.value}));
                        operations.append(mode, resourceControls(member, relation), button('撤销关系', () => post(`/${relation.relationship_id}/revoke`)));
                    } else if (member.member_kind === 'MasterLocal') {
                        operations.append(masterLocalControls(member));
                    }
                    body.appendChild(row);
                }
            } else {
                for (const relation of node.relationships || []) {
                    const row = document.createElement('tr'); const summary = relation.summary || {};
                    cell(row, `${relation.peer_id}\n${relation.peer_endpoint}`);
                    cell(row, `${relation.state} / ${relation.status}\n${relation.rtt_ms} ms`);
                    cell(row, `由 Master 配置\nPhysical free ${gib(summary.storage_free)}`);
                    cell(row, `由 Master 配置`);
                    const operations = cell(row, `${relation.mode}\n业务由 ${relation.direction === 'upstream' ? 'Master' : '当前节点'} 管理`);
                    operations.append(button('撤销关系', () => post(`/${relation.relationship_id}/revoke`))); body.appendChild(row);
                }
            }
        });
        refreshQueue = current.catch(() => {}); return current;
    }
    element('nodesRefresh').onclick = () => action(async () => {});
    panel.querySelector('.module-heading').addEventListener('click', () => { if (panel.classList.contains('expanded')) refresh().catch(error => status(error.message)); });
    element('nodePromotion').onsubmit = event => {
        event.preventDefault(); const role = element('nodeRole').value;
        action(() => post('/promote', {role, endpoint: element('nodeEndpoint').value,
            local_capacity_gib: role === 'Master' ? Number(element('masterLocalCapacity').value) : null}));
    };
    element('nodeIssuePair').onclick = () => action(async () => { element('nodePairPackage').value = JSON.stringify(await post('/pair-package'), null, 2); });
    element('nodeImportPair').onclick = () => action(() => post('/pair', {package: JSON.parse(element('nodePairPackage').value)}));
    element('nodeReinitialize').onsubmit = event => { event.preventDefault(); action(() => post('/reinitialize', {confirmation: element('nodeResetConfirmation').value})); };
})();
