/* Admin-only node controls and real browser media diagnostics. */
(() => {
    const element = id => document.getElementById(id);
    const panel = element('nodesPanel');
    if (!panel) return;
    let node = null;
    let resources = [];
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
        try { status('处理中'); await work(); status('已完成'); await refresh(); }
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
            node = await api('/api/v1/media/admin/nodes');
            element('nodeIdentity').textContent = `${node.role} · ${node.node_id} · ${node.app_version} / v${node.protocol}`;
            visible('nodePromotion', node.role === 'Standalone');
            visible('nodePairing', node.role !== 'Standalone');
            visible('nodeIssuePair', node.role === 'Slave');
            visible('nodeImportPair', node.role === 'Master');
            visible('nodeReinitialize', node.role !== 'Standalone');
            visible('nodeBusinessTest', node.role === 'Master');
            element('nodePairPackage').readOnly = node.role === 'Slave';
            const body = element('nodeRelationships'); body.replaceChildren();
            const choices = element('nodeTestRelationship');
            const selected = choices.value; choices.replaceChildren();
            for (const relation of node.relationships) {
                const row = document.createElement('tr');
                const heartbeat = relation.last_heartbeat ? new Date(relation.last_heartbeat * 1000).toLocaleString() : '尚无心跳';
                const summary = relation.summary || {};
                for (const text of [
                    `${relation.peer_id}\n${relation.peer_endpoint}`,
                    `${relation.state} / ${relation.status}\n${relation.rtt_ms} ms · ${heartbeat}\n失败 ${relation.failures} / 恢复 ${relation.recoveries}`,
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
                    choices.appendChild(option(relation.relationship_id, `${relation.peer_id} · ${relation.mode}`));
                } else {
                    const mode = document.createElement('span'); mode.textContent = relation.mode; operations.appendChild(mode);
                }
                operations.appendChild(button('撤销关系', () => post(`/${relation.relationship_id}/revoke`)));
                row.appendChild(operations); body.appendChild(row);
            }
            if ([...choices.options].some(item => item.value === selected)) choices.value = selected;
            if (node.role === 'Master') await loadResources();
        } finally { loading = false; }
    }
    async function loadResources() {
        const identifier = element('nodeTestRelationship').value;
        const select = element('nodeTestResource'); select.replaceChildren(); resources = [];
        if (!identifier) return;
        const result = await api(`/api/v1/media/admin/nodes/${identifier}/resources`);
        resources = result.items;
        for (const resource of resources) select.appendChild(option(resource.resource_id, resource.path));
    }
    const selectedResource = () => resources.find(resource => resource.resource_id === element('nodeTestResource').value);
    const report = value => { element('nodeTestResult').textContent = JSON.stringify(value, null, 2); };
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
    element('nodeTestRelationship').onchange = () => loadResources().catch(error => status(error.message));
    element('nodeRunTest').onclick = async () => {
        const resource = selectedResource(); if (!resource) return status('请选择媒体');
        try {
            const start = performance.now();
            const head = await fetch(resource.url, {method: 'HEAD', credentials: 'omit', cache: 'no-store'});
            const first = performance.now();
            const range = await fetch(resource.url, {headers: {Range: 'bytes=0-1'}, credentials: 'omit', cache: 'no-store'});
            const bytes = new Uint8Array(await range.arrayBuffer());
            const mode = node.relationships.find(item => item.relationship_id === element('nodeTestRelationship').value)?.mode;
            report({configured_mode: mode, actual_route: new URL(range.url).origin === location.origin ? 'Relay' : 'Direct',
                head_status: head.status, range_status: range.status, head_ms: +(first - start).toFixed(1),
                range_first_bytes_ms: +(performance.now() - first).toFixed(1), content_range: range.headers.get('content-range'), bytes: bytes.length,
                result: head.ok && range.status === 206 && bytes.length === 2 ? '通过' : '失败'});
            element('nodeTestPlayer').src = resource.url;
        } catch (error) { report({result: '失败', detail: error.message}); }
    };
    element('nodeSeekTest').onclick = async () => {
        const player = element('nodeTestPlayer');
        try {
            const before = player.currentTime; player.pause();
            player.currentTime = Math.min(before + 5, Math.max(0, player.duration - 1));
            await player.play(); report({result: '已恢复', paused: player.paused, before, requested_seek: player.currentTime, media_error: player.error?.code || null});
        } catch (error) { report({result: '失败', detail: error.message, media_error: player.error?.code || null}); }
    };
    element('nodeRestartTest').onclick = async () => {
        const resource = selectedResource(); if (!resource) return;
        const player = element('nodeTestPlayer'); player.src = resource.url; player.load();
        try { await player.play(); report({result: '已重新播放', paused: player.paused, media_error: player.error?.code || null}); }
        catch (error) { report({result: '失败', detail: error.message}); }
    };
})();
