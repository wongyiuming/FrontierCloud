/* Admin node operations: Storage, Backup and connection state. */
(() => {
    const $ = id => document.getElementById(id);
    const panel = $('nodesPanel');
    if (!panel) return;

    if (!document.querySelector('link[data-node-observability]')) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = '/static/css/nodes-observability.css?v=20260927';
        link.dataset.nodeObservability = '1';
        document.head.append(link);
    }

    let refreshTimer = null;
    let refreshQueue = Promise.resolve();
    const certificateViews = new Map();
    const setStatus = text => { $('nodeOperationStatus').textContent = text || ''; };
    const visible = (id, show) => $(id).classList.toggle('hidden', !show);
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const gib = value => `${(Number(value || 0) / 1073741824).toFixed(2)} GiB`;
    const dataSize = value => {
        const bytes = Math.max(0, Number(value || 0));
        if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(2)} GiB`;
        if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(2)} MiB`;
        if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
        return `${Math.round(bytes)} B`;
    };
    const localTime = stamp => stamp ? new Date(Number(stamp) * 1000).toLocaleString() : '从未';
    const shortId = value => value ? String(value).slice(0, 12) : '-';
    const post = (path, value = {}) => api(`/api/v1/media/admin/nodes${path}`, {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(value),
    });

    function storageFacts(source = {}) {
        const total = Number(source.physical_total_bytes || 0);
        const free = Number(source.physical_free_bytes || 0);
        return {
            total,
            physicalUsed: Math.max(0, total - free),
            allocated: Number(source.current_allocated_bytes ?? source.allocated_bytes ?? 0),
            used: Number(source.project_used_bytes ?? source.used_bytes ?? 0),
        };
    }

    function storageText(source = {}) {
        const facts = storageFacts(source);
        return `物理 used / all ${gib(facts.physicalUsed)} / ${gib(facts.total)} · 已使用 / 已分配 ${gib(facts.used)} / ${gib(facts.allocated)}`;
    }

    function relativeTime(stamp) {
        if (!stamp) return '从未';
        const delta = Math.round(Number(stamp) - Date.now() / 1000);
        const seconds = Math.abs(delta);
        let value;
        if (seconds < 60) value = `${seconds} 秒`;
        else if (seconds < 3600) value = `${Math.floor(seconds / 60)} 分钟`;
        else if (seconds < 86400) value = `${Math.floor(seconds / 3600)} 小时`;
        else value = `${Math.floor(seconds / 86400)} 天`;
        return delta > 0 ? `${value}后` : `${value}前`;
    }

    function duration(seconds) {
        const value = Math.max(0, Number(seconds || 0));
        if (value < 60) return `${Math.round(value)} 秒`;
        if (value < 3600) return `${Math.floor(value / 60)} 分钟`;
        if (value < 86400) return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分`;
        return `${Math.floor(value / 86400)} 天 ${Math.floor((value % 86400) / 3600)} 小时`;
    }

    function make(tag, className = '', text = '') {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== '') node.textContent = text;
        return node;
    }

    function pill(text, tone = 'muted') { return make('span', `node-pill ${tone}`, text); }

    function kv(items) {
        const grid = make('div', 'node-kv');
        for (const [label, value] of items) grid.append(make('span', '', label), make('span', '', String(value)));
        return grid;
    }

    function syncLine(value) {
        const labels = {
            effective: ['已生效', 'effective'], syncing: ['同步中', 'syncing'],
            awaiting: ['等待确认', 'syncing'], offline: ['节点离线', 'offline'],
        };
        const [label, tone] = labels[value] || ['未知', ''];
        const row = make('div', `node-sync-line ${tone}`);
        row.append(make('i', 'node-sync-dot'), make('span', '', label));
        return row;
    }

    async function action(work, defaultMessage = '已完成') {
        try {
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

    async function loadData() {
        const node = await api('/api/v1/media/admin/nodes');
        let observability = {members: [], relationships: []};
        try { observability = await api('/api/v1/media/admin/nodes/observability'); }
        catch (error) { console.warn('node observability unavailable', error); }
        return {node, observability};
    }

    function ensureOverview() {
        let overview = $('nodeFleetOverview');
        if (overview) return overview;
        overview = make('div', 'node-fleet-overview');
        overview.id = 'nodeFleetOverview';
        panel.querySelector('.nodes-table-scroll').before(overview);
        return overview;
    }

    function overviewCard(label, value, detail, tone) {
        const card = make('div', `node-overview-stat ${tone || ''}`);
        card.append(make('small', '', label), make('strong', '', value), make('span', '', detail));
        return card;
    }

    function renderOverview(node, observed) {
        const overview = ensureOverview();
        overview.replaceChildren();
        if (node.role !== 'Master') {
            const online = (node.relationships || []).filter(item => item.status === 'online').length;
            overview.append(
                overviewCard('当前角色', node.role, '资源策略由 Master 控制', 'good'),
                overviewCard('上游关系', String((node.relationships || []).length), `${online} 条在线`, 'storage'),
                overviewCard('协议版本', `v${node.protocol}`, node.app_version, 'storage'),
            );
            return;
        }
        const pool = node.storage_pool || {};
        const followers = (observed.members || []).filter(item => item.member_kind === 'Follower');
        const online = followers.filter(item => item.connection?.status === 'online').length;
        const backupEnabled = followers.filter(item => item.backup?.enabled).length;
        const backupHealthy = followers.filter(item => item.backup?.health === 'healthy').length;
        const poolFacts = storageFacts(pool);
        overview.append(
            overviewCard('存储节点', `${online} / ${followers.length}`, online === followers.length ? '全部在线' : '存在离线或降级节点', 'good'),
            overviewCard('Storage', `${gib(poolFacts.physicalUsed)} / ${gib(poolFacts.total)}`, `物理 used / all · 已使用 / 已分配 ${gib(poolFacts.used)} / ${gib(poolFacts.allocated)}`, 'storage'),
            overviewCard('Backup', `${backupHealthy} / ${backupEnabled}`, backupEnabled ? '健康 / 已启用备份节点' : '尚未启用备份节点', 'backup'),
        );
    }

    function connectionPanel(relation, observed) {
        const connection = observed?.connection || {};
        const state = connection.status || relation?.status || 'local';
        const section = make('section', 'node-resource-panel connection');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', '连接状态'), pill(state === 'online' ? 'Online' : state, state === 'online' ? 'good' : state === 'local' ? 'info' : 'bad'));
        const current = Number(connection.current_rtt_ms ?? relation?.rtt_ms ?? 0);
        section.append(heading, make('div', 'node-metric-primary', state === 'local' ? 'Local' : `${current} ms`));
        if (state !== 'local') section.append(kv([
            ['最近心跳', relativeTime(connection.last_heartbeat || relation?.last_heartbeat)],
            ['最近时间', localTime(connection.last_heartbeat || relation?.last_heartbeat)],
            ['1h 平均', `${connection.avg_rtt_ms || current} ms`],
            ['1h 最低 / 最高', `${connection.min_rtt_ms || current} / ${connection.max_rtt_ms || current} ms`],
            ['RTT 样本', `${connection.sample_count || (current ? 1 : 0)} 次`],
            ['连续失败', connection.failures ?? relation?.failures ?? 0],
            ['历史恢复', connection.recoveries ?? relation?.recoveries ?? 0],
        ]));
        section.append(make('div', 'node-note', state === 'local' ? 'Master Local 是本机资源。' : 'RTT 是 Master 到该节点 HTTPS 控制面的往返时间。'));
        return section;
    }

    function storagePanel(member, observed) {
        const section = make('section', 'node-resource-panel storage');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', 'Storage'), pill(member.storage_enabled ? 'Enabled' : 'Disabled', member.storage_enabled ? 'info' : 'muted'));
        const facts = storageFacts(member);
        section.append(heading, make('div', 'node-metric-primary', `${gib(facts.physicalUsed)} / ${gib(facts.total)}`), kv([
            ['物理 used / all', `${gib(facts.physicalUsed)} / ${gib(facts.total)}`],
            ['已使用 / 已分配', `${gib(facts.used)} / ${gib(facts.allocated)}`],
        ]), syncLine(observed?.sync?.storage || (member.member_kind === 'MasterLocal' ? 'effective' : 'awaiting')),
        make('div', 'node-note', '物理表示该媒体文件系统已用 / 总容量；已使用 / 已分配表示 FrontierCloud 项目占用 / 分配配额。'));
        return section;
    }

    function backupPanel(member, observed) {
        const backup = observed?.backup || member.backup || {};
        const health = backup.health || 'disabled';
        const labels = {healthy: 'Healthy', running: 'Backing up', failed: 'Failed', stale: 'Stale', 'waiting-first-backup': 'Waiting first backup', disabled: 'Disabled'};
        const tone = health === 'healthy' ? 'good' : (health === 'running' || health === 'waiting-first-backup' ? 'warn' : (health === 'failed' || health === 'stale') ? 'bad' : 'muted');
        const section = make('section', 'node-resource-panel backup');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', 'Backup'), pill(labels[health] || backup.raw_state || 'Unknown', tone));
        section.append(heading, make('div', 'node-metric-primary', backup.last_success ? relativeTime(backup.last_success) : (backup.enabled ? '尚未成功' : '未启用')), kv([
            ['最近成功', localTime(backup.last_success)],
            ['距今', backup.last_success ? duration(backup.lag_seconds) : '-'],
            ['最近大小', backup.last_size_bytes ? dataSize(backup.last_size_bytes) : '-'],
            ['恢复点', `${backup.recovery_points || 0} 个`],
            ['最近尝试', backup.last_attempt ? `${localTime(backup.last_attempt)} · ${backup.last_attempt_state || '-'}` : '-'],
            ['下次计划', backup.next_due ? `${localTime(backup.next_due)} · ${relativeTime(backup.next_due)}` : '-'],
            ['最近校验', backup.checksum ? `SHA256 ${shortId(backup.checksum)}…` : '-'],
        ]), syncLine(observed?.sync?.backup || (member.member_kind === 'MasterLocal' ? 'effective' : 'awaiting')),
        make('div', 'node-note', 'Master 每 24 小时向启用 Backup 的存储节点 写入业务恢复点；失败约 5 分钟后重试。'));
        return section;
    }

    function configBox(member) {
        const grid = make('div', 'node-config-grid');
        const storageBox = make('div', 'node-config-box');
        const storageEnabled = document.createElement('input');
        storageEnabled.type = 'checkbox'; storageEnabled.checked = Boolean(member.storage_enabled);
        const capacity = document.createElement('input');
        capacity.type = 'number'; capacity.min = '1'; capacity.max = '10240';
        capacity.value = String(Math.max(1, Math.ceil(Number(member.allocated_bytes || 0) / 1073741824)));
        capacity.disabled = !storageEnabled.checked;
        storageEnabled.onchange = () => { capacity.disabled = !storageEnabled.checked; };
        const storageLabel = make('label'); storageLabel.append(storageEnabled, document.createTextNode(' Storage 承载业务数据'));
        const inputs = make('div', 'node-config-inputs'); inputs.append(capacity, make('span', '', 'GiB 配额'));
        storageBox.append(storageLabel, make('small', '', '关闭后停止新写入；已有数据不会自动迁走。'), inputs);

        const backupBox = make('div', 'node-config-box');
        const backupEnabled = document.createElement('input');
        backupEnabled.type = 'checkbox'; backupEnabled.checked = Boolean(member.backup?.enabled);
        const backupLabel = make('label'); backupLabel.append(backupEnabled, document.createTextNode(' Backup 恢复点'));
        backupBox.append(backupLabel, make('small', '', 'Master 每 24h 发送业务恢复包；失败后约 5 分钟重试。'));
        grid.append(storageBox, backupBox);
        return {grid, storageEnabled, capacity, backupEnabled};
    }

    async function waitForEffective(memberId, timeoutMs = 75000) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            await sleep(2200);
            const data = await api('/api/v1/media/admin/nodes/observability');
            const member = (data.members || []).find(item => item.member_id === memberId);
            if (!member) continue;
            if (member.sync?.storage === 'effective' && member.sync?.backup === 'effective') return `配置已在 存储节点生效 · ${new Date().toLocaleTimeString()}`;
            if (member.connection?.status === 'offline') return 'Master 已保存配置，但 存储节点当前离线，尚未生效';
        }
        return 'Master 已保存配置；存储节点尚未在心跳中确认，请检查连接状态';
    }

    function controls(member, relation) {
        const shell = make('div', 'node-card-controls');
        if (!relation || relation.state !== 'active') {
            if (member.member_kind === 'MasterLocal') {
                const input = document.createElement('input');
                input.type = 'number'; input.min = '1'; input.max = '10240'; input.style.width = '90px';
                input.value = String(Math.max(1, Math.ceil(Number(member.allocated_bytes || 0) / 1073741824)));
                const actions = make('div', 'node-card-actions');
                actions.append(input, button('更新本机配额', () => post(`/${member.member_id}/resources`, {storage_enabled: true, storage_capacity_gib: Number(input.value), backup_enabled: false}), 'apply'));
                shell.append(make('div', 'node-note', 'Master Local 是本机固定资源，仅允许调整 Storage 配额。'), actions);
            }
            return shell;
        }
        const config = configBox(member);
        const actions = make('div', 'node-card-actions');
        const mode = document.createElement('select');
        mode.setAttribute('aria-label', `${member.member_id} 数据传输模式`);
        mode.title = 'Relay 由 Master 中转业务数据；Direct 允许数据面直接访问 存储节点。';
        for (const value of ['Relay', 'Direct']) { const option = document.createElement('option'); option.value = value; option.textContent = value; mode.append(option); }
        mode.value = relation.mode;
        if (relation.summary?.storage_transport) { mode.disabled = true; mode.title = '网络部署类型固定：直连使用 443，中继使用通信高端口；切换需要维护期重新部署。'; }
        mode.onchange = () => action(() => post(`/${relation.relationship_id}/mode`, {mode: mode.value}));
        actions.append(mode, button('应用资源配置', async () => {
            await post(`/${relation.relationship_id}/resources`, {
                storage_enabled: config.storageEnabled.checked,
                storage_capacity_gib: config.storageEnabled.checked ? Number(config.capacity.value) : 0,
                backup_enabled: config.backupEnabled.checked,
            });
            return await waitForEffective(member.member_id);
        }, 'apply', '只有 存储节点回报的 Observed 配置与 Desired 一致才显示已生效。'),
        button('撤销关系', () => post(`/${relation.relationship_id}/revoke`), 'danger',
            '存储节点仍持有有效 Storage Pool 文件时后端会拒绝撤销。'));
        shell.append(config.grid, actions);
        return shell;
    }

    function memberCard(member, relation, observed) {
        const row = document.createElement('tr'); row.className = 'node-card-row';
        const cell = document.createElement('td'); cell.colSpan = 4;
        const card = make('article', 'node-card');
        const header = make('header', 'node-card-header');
        const title = make('div', 'node-card-title');
        title.append(make('strong', '', member.member_kind === 'MasterLocal' ? 'Master Local' : member.member_id), make('small', '', relation?.peer_endpoint || '本机资源'));
        const badges = make('div', 'node-card-badges');
        badges.append(pill(member.member_kind === 'MasterLocal' ? 'LOCAL' : String(relation?.status || 'UNKNOWN').toUpperCase(), member.member_kind === 'MasterLocal' ? 'info' : relation?.status === 'online' ? 'good' : 'bad'), pill(member.transport || relation?.mode || 'Local', 'muted'));
        header.append(title, badges);
        const grid = make('div', 'node-card-grid');
        grid.append(connectionPanel(relation, observed), storagePanel(member, observed), backupPanel(member, observed));
        if (relation?.direction === 'downstream' && relation.state === 'active') grid.append(certificatePanel(relation));
        card.append(header, grid, controls(member, relation)); cell.append(card); row.append(cell);
        return row;
    }

    function certificatePanel(relation) {
        const section = make('section', 'node-resource-panel');
        const heading = make('div', 'node-resource-heading');
        heading.append(make('strong', '', relation.local ? '本机证书' : '存储节点证书'));
        const observed = relation.summary?.certificate || {};
        const queried = certificateViews.get(relation.relationship_id);
        const facts = queried && Date.now() - queried.at < 30000 ? queried.value : observed;
        const labels = {DISABLED: '未启用 TLS', PENDING: '等待证书', VALIDATING: '验证域名', ISSUING: '签发中', ACTIVE: '已生效', RENEWING: '续期中', DEGRADED: '降级，需检查', FAILED: '失败'};
        const codes = {dns: 'DNS 未就绪或解析失败', port80_busy: '80 端口被占用', http01_unreachable: '公网 HTTP-01 不可达', http01_ingress: '等待内部 Challenge 入口就绪', authorization: '域名授权失败', caa: 'CAA 限制', rate_limited: 'CA 限流，自动退避', ca_unavailable: 'CA 暂不可用', ca_timeout: 'CA 请求超时', ca_error: 'CA 请求失败', domain_rejected: 'CA 拒绝域名', system_clock: '系统时间异常', interrupted: '上次操作中断', journal_unwritable: '状态无法持久化', journal_recovered: '已保护损坏记录，等待安全重试', certificate_cache_unreadable: '证书缓存无法读取，保留有效旧证书', certificate_not_yet_valid: '证书尚未生效，请检查系统时间', certificate_cache_unwritable: '签发结果无法保存，保留旧证书', legacy_certificate_invalid: '历史证书无法安全迁移', challenge_listener: 'ACME 验证监听器异常', certificate_validation: '证书完整校验失败', certificate_invalid: '证书已失效或损坏', certificate_expired: '证书已过期', serving_certificate_mismatch: '线上证书尚未与托管证书一致', controller_unavailable: '证书控制器不可用'};
        section.append(heading, kv([
            ['TLS', facts.enabled ? '已启用' : facts.state === 'DISABLED' ? '未启用' : '待查询'],
            ['管理方式', facts.mode === 'automatic' ? '自动托管' : '待查询'],
            ['状态', labels[facts.state] || '待查询'],
            ['域名', facts.hostname || '待查询'],
            ['SAN', Array.isArray(facts.san) ? facts.san.join(', ') : '待查询'],
            ['签发机构', facts.issuer || '待查询'],
            ['生效时间', localTime(facts.not_before)],
            ['有效期至', localTime(facts.not_after)],
            ['剩余有效期', facts.not_after ? duration(facts.remaining_seconds) : '待查询'],
            ['最近续期', facts.last_renewal === 'success' ? '成功' : facts.last_renewal === 'failed' ? '失败' : '尚无续期记录'],
            ['下次计划续期', facts.next_renewal ? localTime(facts.next_renewal) : '待签发或等待 CA 计划'],
            ['最近错误', facts.last_error ? codes[facts.last_error] || '未知错误' : '无'],
            ['HTTPS 服务', facts.https_ready ? '握手已验证' : '尚未验证可用'],
            ['线上证书一致', facts.serving_matches ? '已验证一致' : '尚未验证一致'],
            ['控制器', facts.controller_running ? '运行中' : '尚未确认'],
            ['验证入口', facts.validation === 'temporary_port80' ? '临时监听 80，完成即释放' : facts.validation === 'http01' ? 'HTTP-01' : '待查询'],
        ]));
        const buttons = make('div', 'node-card-controls');
        const endpoint = `/${encodeURIComponent(relation.relationship_id)}/certificate`;
        const query = button('查询证书', async () => {
            const state = await api(`/api/v1/media/admin/nodes${endpoint}`);
            certificateViews.set(relation.relationship_id, {at: Date.now(), value: state});
            relation.summary = {...relation.summary, certificate: state};
            section.replaceWith(certificatePanel(relation));
            return `证书${labels[state.state] || '待查询'}，有效期至 ${localTime(state.not_after)}`;
        });
        query.disabled = !observed.state;
        buttons.append(query);
        const download = button('下载证书及私钥', () => downloadCertificate(endpoint, facts));
        download.disabled = !facts.enabled || !facts.https_ready || !facts.serving_matches;
        buttons.append(download);
        section.append(buttons);
        return section;
    }

    async function downloadCertificate(endpoint, facts) {
        if (!window.isSecureContext || !crypto.subtle) throw new Error('证书下载需要安全的浏览器环境');
        const encode = bytes => btoa(Array.from(bytes, value => String.fromCharCode(value)).join('')).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
        const decode = value => Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), char => char.charCodeAt(0));
        const keys = await crypto.subtle.generateKey({name: 'ECDH', namedCurve: 'P-256'}, false, ['deriveBits']);
        const browserPublic = encode(new Uint8Array(await crypto.subtle.exportKey('raw', keys.publicKey)));
        const browserNonce = encode(crypto.getRandomValues(new Uint8Array(32)));
        const state = await post(endpoint, {action: 'download', browser_public_key: browserPublic, browser_nonce: browserNonce});
        const envelope = state.download;
        if (!envelope || envelope.algorithm !== 'P256-HKDF-SHA256-AES256GCM' || envelope.hostname !== facts.hostname || envelope.browser_nonce !== browserNonce || envelope.fingerprint !== state.fingerprint || !/^[a-f0-9]{64}$/.test(envelope.fingerprint) || envelope.ciphertext.length > 120000) throw new Error('证书下载身份校验失败');
        const aad = new TextEncoder().encode(['frontiercloud.certificate.download.v1', envelope.hostname, envelope.fingerprint, browserNonce].join('\n'));
        const peer = await crypto.subtle.importKey('raw', decode(envelope.public_key), {name: 'ECDH', namedCurve: 'P-256'}, false, []);
        const secret = new Uint8Array(await crypto.subtle.deriveBits({name: 'ECDH', public: peer}, keys.privateKey, 256));
        const material = await crypto.subtle.importKey('raw', secret, 'HKDF', false, ['deriveKey']); secret.fill(0);
        const wrapping = await crypto.subtle.deriveKey({name: 'HKDF', hash: 'SHA-256', salt: decode(envelope.salt), info: aad}, material, {name: 'AES-GCM', length: 256}, false, ['decrypt']);
        const raw = new Uint8Array(await crypto.subtle.decrypt({name: 'AES-GCM', iv: decode(envelope.iv), additionalData: aad}, wrapping, decode(envelope.ciphertext)));
        let pair;
        try { pair = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(raw)); } finally { raw.fill(0); }
        if (typeof pair.certificate !== 'string' || pair.certificate.length > 65536 || typeof pair.private_key !== 'string' || pair.private_key.length > 16384 || !pair.private_key.includes('PRIVATE KEY-----')) throw new Error('证书下载内容无效');
        const leaf = pair.certificate.match(/-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/\s=]+?)\s*-----END CERTIFICATE-----/);
        if (!leaf) throw new Error('证书下载内容无效');
        const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', decode(leaf[1].replace(/\s/g, ''))));
        if (Array.from(hash, value => value.toString(16).padStart(2, '0')).join('') !== envelope.fingerprint) throw new Error('证书下载指纹不匹配');
        const url = URL.createObjectURL(new Blob([pair.certificate, '\n', pair.private_key], {type: 'application/x-pem-file'}));
        pair = null;
        const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${envelope.hostname}-tls-key-pair.pem`; document.body.append(anchor); anchor.click(); anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        return '已下载此节点的证书及私钥 PEM 文件';
    }

    function renderRows(node, observed) {
        const body = $('nodeRelationships'); body.replaceChildren();
        if (node.certificate) {
            const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 4;
            cell.append(certificatePanel({local: true, relationship_id: node.node_id, summary: {certificate: node.certificate}})); row.append(cell); body.append(row);
        }
        const relations = new Map((node.relationships || []).map(item => [item.peer_id, item]));
        const observedMembers = new Map((observed.members || []).map(item => [item.member_id, item]));
        if (node.storage_pool) {
            for (const member of node.storage_pool.members || []) body.append(memberCard(member, relations.get(member.member_id), observedMembers.get(member.member_id)));

        }
    }

    async function refresh() {
        const current = refreshQueue.then(async () => {
            const {node, observability} = await loadData();
            $('nodeIdentity').textContent = `${node.role} · ${node.node_id} · ${node.app_version} / v${node.protocol}${node.endpoint ? ' · ' + node.endpoint : ''}`;
            visible('nodePromotion', node.role === 'Standalone'); visible('nodePairing', node.role !== 'Standalone');
            visible('nodeImportPair', node.role === 'Master'); visible('nodeReinitialize', node.role !== 'Standalone');
            $('nodePairPackage').readOnly = false;
            const role = $('nodeRole'), capacity = $('masterLocalCapacity'); capacity.disabled = role.value !== 'Master'; role.onchange = () => { capacity.disabled = role.value !== 'Master'; };
            renderOverview(node, observability);
            const pool = node.storage_pool;
            $('storagePoolSummary').textContent = pool ? `Storage Pool · ${storageText(pool)}` : `${node.role} · 资源策略由 Master 管理`;
            renderRows(node, observability);
        });
        refreshQueue = current.catch(() => {}); return current;
    }

    function startAutoRefresh() {
        if (refreshTimer) return;
        refreshTimer = setInterval(() => { if (panel.classList.contains('expanded')) refresh().catch(error => setStatus(error.message)); }, 10000);
    }
    function stopAutoRefresh() { if (refreshTimer) clearInterval(refreshTimer); refreshTimer = null; }

    $('nodesRefresh').onclick = () => action(async () => {}, '已刷新');
    panel.querySelector('.module-heading').addEventListener('click', () => { if (panel.classList.contains('expanded')) { refresh().catch(error => setStatus(error.message)); startAutoRefresh(); } else stopAutoRefresh(); });
    $('nodePromotion').onsubmit = event => { event.preventDefault(); const role = $('nodeRole').value; action(() => post('/promote', {role, endpoint: $('nodeEndpoint').value, local_capacity_gib: role === 'Master' ? Number($('masterLocalCapacity').value) : null})); };
    $('nodeImportPair').onclick = () => action(() => post('/pair', {package: JSON.parse($('nodePairPackage').value)}));
    $('nodeReinitialize').onsubmit = event => { event.preventDefault(); action(() => post('/reinitialize', {confirmation: $('nodeResetConfirmation').value})); };
})();
