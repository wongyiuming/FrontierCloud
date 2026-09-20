/* Admin-only node identity and federation relationship controls. */
(() => {
    const element = id => document.getElementById(id);
    const panel = element('nodesPanel');
    if (!panel) return;
    let loading = false;
    const status = text => { element('nodeOperationStatus').textContent = text; };
    const visible = (id, show) => element(id).classList.toggle('hidden', !show);
    const post = (path, value = {}) => api(`/api/v1/media/admin/nodes${path}`, {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(value),
    });
    const option = (value, label) => {
        const result = document.createElement('option');
        result.value = value; result.textContent = label; return result;
    };
    async function action(work) {
        try { status('处理中'); await work(); await refresh(); status('已完成'); }
        catch (error) { status(error.message); }
    }
    function button(label, work) {
        const result = document.createElement('button');
        result.type = 'button'; result.textContent = label;
        result.onclick = () => action(work); return result;
    }
    async function refresh() {
        if (loading) return;
        loading = true;
        try {
            const node = await api('/api/v1/media/admin/nodes');
            element('nodeIdentity').textContent = `${node.role} · ${node.node_id} · ${node.app_version} / v${node.protocol}${node.endpoint ? ' · ' + node.endpoint : ''}`;
            visible('nodePromotion', node.role === 'Standalone');
            visible('nodePairing', node.role !== 'Standalone');
            visible('nodeIssuePair', node.role === 'Slave');
            visible('nodeImportPair', node.role === 'Master');
            visible('nodeReinitialize', node.role !== 'Standalone');
            element('nodePairPackage').readOnly = node.role === 'Slave';
            const body = element('nodeRelationships'); body.replaceChildren();
            for (const relation of node.relationships) {
                const row = document.createElement('tr');
                const heartbeat = relation.last_heartbeat ? new Date(relation.last_heartbeat * 1000).toLocaleString() : '尚无心跳';
                const summary = relation.summary || {};
                const recovered = summary.recovered_at ? new Date(summary.recovered_at * 1000).toLocaleString() : '-';
                for (const text of [
                    `${relation.peer_id}\n${relation.peer_endpoint}`,
                    `${relation.state} / ${relation.status}\n${relation.rtt_ms} ms · ${heartbeat}\n失败 ${relation.failures} / 恢复 ${relation.recoveries} · ${recovered}`,
                    `${summary.app_version || relation.peer_version} / v${relation.protocol}\n同步 ${relation.cursor} / ${summary.catalog_version || 0} · 媒体 ${summary.media_count || 0}\n可用空间 ${summary.storage_free == null ? '-' : (summary.storage_free / 1073741824).toFixed(2) + ' GiB'}`,
                ]) {
                    const cell = document.createElement('td'); cell.textContent = text; cell.style.whiteSpace = 'pre-line'; row.appendChild(cell);
                }
                const operations = document.createElement('td');
                if (node.role === 'Master' && relation.state === 'active') {
                    const mode = document.createElement('select');
                    mode.setAttribute('aria-label', `关系 ${relation.peer_id} 的传输模式`);
                    for (const value of ['Relay', 'Direct']) mode.appendChild(option(value, value));
                    mode.value = relation.mode;
                    mode.onchange = () => action(() => post(`/${relation.relationship_id}/mode`, {mode: mode.value}));
                    operations.append(mode, button('修复同步', () => post(`/${relation.relationship_id}/sync`)));
                } else {
                    const mode = document.createElement('span'); mode.textContent = relation.mode; operations.appendChild(mode);
                }
                operations.appendChild(button('撤销关系', () => post(`/${relation.relationship_id}/revoke`)));
                row.appendChild(operations); body.appendChild(row);
            }
        } finally { loading = false; }
    }
    element('nodesRefresh').onclick = () => action(async () => {});
    panel.querySelector('.module-heading').addEventListener('click', () => {
        if (panel.classList.contains('expanded')) refresh().catch(error => status(error.message));
    });
    element('nodePromotion').onsubmit = event => {
        event.preventDefault(); action(() => post('/promote', {role: element('nodeRole').value, endpoint: element('nodeEndpoint').value}));
    };
    element('nodeIssuePair').onclick = () => action(async () => {
        element('nodePairPackage').value = JSON.stringify(await post('/pair-package'), null, 2);
    });
    element('nodeImportPair').onclick = () => action(() => post('/pair', {package: JSON.parse(element('nodePairPackage').value)}));
    element('nodeReinitialize').onsubmit = event => {
        event.preventDefault(); action(() => post('/reinitialize', {confirmation: element('nodeResetConfirmation').value}));
    };
})();
