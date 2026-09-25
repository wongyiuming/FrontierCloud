(() => {
    const PANEL_ID = 'systemVersionPanel';
    const RELEASE_BASE = '/api/v1/media/admin/nodes/release';
    const PHASE_PROGRESS = {
        idle: 0,
        queued: 4,
        validating: 12,
        building: 38,
        replacing: 66,
        distributing: 82,
        complete: 100,
        failed: 100,
        unavailable: 0,
    };
    const PHASE_LABEL = {
        idle: '待机', queued: '已排队', validating: '校验版本', building: '构建镜像',
        replacing: '替换服务', distributing: '集群分发', complete: '完成', failed: '失败',
        unavailable: '不可用',
    };
    const STEP_PHASES = ['validating', 'building', 'replacing', 'distributing', 'complete'];
    const STEP_LABELS = ['校验', '构建', '替换', '分发', '完成'];
    let timer = null;
    let lastValue = null;

    const element = id => document.getElementById(id);
    const shortSha = value => value ? String(value).slice(0, 12) : '-';
    const busy = local => ['queued', 'running', 'distributing'].includes(local?.state);
    const phaseProgress = release => {
        if (!release) return 0;
        if (release.state === 'success' && release.phase === 'complete') return 100;
        if (release.state === 'failed') {
            return release.current_sha && release.target_sha && release.current_sha === release.target_sha ? 88 : 28;
        }
        return PHASE_PROGRESS[release.phase] ?? (release.state === 'running' ? 10 : 0);
    };

    function setExpanded(panel) {
        const open = !panel.classList.contains('expanded');
        for (const module of document.querySelectorAll('.admin-module')) {
            const selected = module === panel && open;
            module.classList.toggle('expanded', selected);
            const heading = module.querySelector('.module-heading');
            if (!heading) continue;
            heading.setAttribute('aria-expanded', String(selected));
            const marker = heading.querySelector('b');
            if (marker) marker.textContent = selected ? '−' : '＋';
        }
        return open;
    }

    function ciText(value) {
        const ci = value.ci || {};
        if (!ci.available) return ci.detail || '发布验证不可用';
        const state = ci.publishable
            ? '通过'
            : ci.status === 'completed'
                ? `失败 · ${ci.conclusion || 'unknown'}`
                : ci.status || 'unknown';
        const releaseBranch = value.release_branch || ci.branch || 'main';
        const sourceBranch = ci.source_branch || 'dev';
        return `${releaseBranch} ${shortSha(ci.sha)} · ${sourceBranch} CI #${ci.run_number || '-'} ${state} · ${shortSha(ci.ci_sha)}`;
    }

    function convergence(value) {
        const target = value.ci?.sha || '';
        const nodes = [{reachable: true, status: value.local || {}, master: true}, ...(value.followers || [])];
        const total = nodes.length;
        const done = nodes.filter(item => {
            if (item.reachable === false) return false;
            const status = item.status || {};
            return status.current_sha === target && status.state === 'success';
        }).length;
        return {done, total};
    }

    function overallProgress(value) {
        const local = value.local || {};
        const target = value.ci?.sha || '';
        const cluster = convergence(value);
        if (target && cluster.total && cluster.done === cluster.total) return 100;
        const base = phaseProgress(local);
        if (local.phase === 'distributing' || (local.current_sha === target && value.cluster_convergence_needed)) {
            return Math.max(base, 72 + Math.round(28 * (cluster.done / Math.max(1, cluster.total))));
        }
        return base;
    }

    function renderSteps(local) {
        const host = element('systemReleaseSteps');
        const current = STEP_PHASES.indexOf(local.phase);
        host.replaceChildren(...STEP_PHASES.map((phase, index) => {
            const item = document.createElement('div');
            item.className = 'release-step';
            item.textContent = STEP_LABELS[index];
            if (local.state === 'failed' && (phase === local.phase || index === Math.max(0, current))) item.classList.add('failed');
            else if (local.state === 'success' || index < current || local.phase === 'complete') item.classList.add('done');
            else if (index === current) item.classList.add('active');
            return item;
        }));
    }

    function nodeCard(name, release, reachable = true, target = '') {
        const row = document.createElement('article');
        const state = reachable ? (release.state || 'unknown') : 'unreachable';
        row.className = `release-node ${state === 'success' ? 'success' : ''} ${state === 'failed' || state === 'unreachable' ? 'failed' : ''}`;

        const identity = document.createElement('div');
        const title = document.createElement('strong'); title.textContent = name;
        const sha = document.createElement('code'); sha.textContent = shortSha(release.current_sha);
        identity.append(title, sha);

        const progress = document.createElement('div');
        const shell = document.createElement('div'); shell.className = 'release-progress-shell';
        const bar = document.createElement('div'); bar.className = 'release-progress-bar';
        let percent = reachable ? phaseProgress(release) : 0;
        if (target && release.current_sha === target && release.state === 'success') percent = 100;
        if (release.state === 'failed') bar.classList.add('failed');
        bar.style.width = `${percent}%`; shell.append(bar);
        const caption = document.createElement('small');
        caption.textContent = reachable
            ? `${state} / ${PHASE_LABEL[release.phase] || release.phase || '-'}${release.target_sha ? ` · target ${shortSha(release.target_sha)}` : ''}`
            : '无法连接';
        progress.append(shell, caption);

        const detail = document.createElement('div');
        const branch = document.createElement('small');
        branch.textContent = `track ${release.release_branch || 'legacy'}${release.previous_sha ? ` · rollback ${shortSha(release.previous_sha)}` : ''}`;
        detail.append(branch);
        if (release.detail) {
            const failure = document.createElement('small');
            failure.textContent = release.detail.length > 240 ? `${release.detail.slice(0, 240)}…` : release.detail;
            detail.append(document.createElement('br'), failure);
        }
        row.append(identity, progress, detail);
        return row;
    }

    function render(value) {
        lastValue = value;
        const ci = value.ci || {};
        const local = value.local || {};
        const cluster = convergence(value);
        const percent = overallProgress(value);

        element('systemReleaseCi').textContent = ciText(value);
        element('systemReleasePolicy').textContent = value.release_policy_ready
            ? `ready · ${value.release_branch || 'main'} HEAD 代码树已通过 ${ci.source_branch || 'dev'} CI`
            : `blocked · ${value.release_policy_detail || '发布策略未就绪'}`;
        element('systemReleaseTarget').textContent = shortSha(ci.sha);
        element('systemReleaseCurrent').textContent = shortSha(local.current_sha);
        element('systemReleaseConvergence').textContent = `${cluster.done} / ${cluster.total}`;
        element('systemReleaseProgressBar').style.width = `${percent}%`;
        element('systemReleaseProgressBar').classList.toggle('failed', local.state === 'failed');
        element('systemReleaseProgressCaption').textContent =
            `${percent}% · Master ${local.state || 'unknown'} / ${PHASE_LABEL[local.phase] || local.phase || '-'}${value.cluster_convergence_needed ? ' · 等待集群收敛' : ''}`;
        renderSteps(local);

        const nodes = [nodeCard('Master', local, true, ci.sha)];
        for (const item of value.followers || []) {
            nodes.push(nodeCard(item.peer_endpoint || item.peer_id || 'Follower', item.status || {}, item.reachable !== false, ci.sha));
        }
        element('systemReleaseNodes').replaceChildren(...nodes);

        const details = [];
        if (ci.detail) details.push(ci.detail);
        if (local.detail) details.push(local.detail);
        for (const item of value.followers || []) {
            if (item.detail) details.push(`${item.peer_endpoint || item.peer_id}: ${item.detail}`);
            if (item.status?.detail) details.push(`${item.peer_endpoint || item.peer_id}: ${item.status.detail}`);
        }
        const error = element('systemReleaseError');
        error.textContent = details.join('\n');
        error.classList.toggle('hidden', !details.length);

        element('systemReleaseUpgrade').disabled = !value.can_upgrade;
        element('systemReleaseRollback').disabled = !value.can_rollback;
        const masterBusy = busy(local);
        element('systemReleaseState').textContent = masterBusy
            ? `执行中 · ${PHASE_LABEL[local.phase] || local.phase || local.state}`
            : local.state === 'failed'
                ? '上次发布失败'
                : value.cluster_convergence_needed
                    ? '集群需要继续收敛'
                    : '发布系统空闲';
        schedule(masterBusy ? 1500 : 5000);
    }

    async function refresh(force = false) {
        const value = await api(`${RELEASE_BASE}${force ? '?refresh_ci=true' : ''}`);
        render(value);
        return value;
    }

    function schedule(delay) {
        if (timer) clearTimeout(timer);
        const panel = element(PANEL_ID);
        if (!panel?.classList.contains('expanded') && !busy(lastValue?.local || {})) {
            timer = null;
            return;
        }
        timer = setTimeout(() => refresh(false).catch(showError), delay);
    }

    function showError(error) {
        const host = element('systemReleaseError');
        if (host) {
            host.textContent = error.message;
            host.classList.remove('hidden');
        }
        schedule(5000);
    }

    async function runAction(path, label) {
        const target = lastValue?.ci?.sha ? shortSha(lastValue.ci.sha) : 'main';
        if (path.endsWith('/rollback') && !confirm(`确认将整个集群回滚到上一 Web 管理版本？\n当前 main 目标：${target}`)) return;
        element('systemReleaseState').textContent = label;
        try {
            await api(path, {method: 'POST', headers: requestHeaders(), body: '{}'});
            await refresh(false);
            schedule(1000);
        } catch (error) {
            showError(error);
        }
    }

    function init() {
        if (element(PANEL_ID)) return;
        const panel = document.createElement('section');
        panel.id = PANEL_ID;
        panel.className = 'admin-module system-version-panel';
        panel.dataset.adminModule = 'release';
        panel.innerHTML = `
            <button class="module-heading" type="button" aria-expanded="false">
                <span><strong>系统版本管理</strong><small id="systemReleaseState">main 发布、集群分发、实时进度与一键回滚</small></span><b>＋</b>
            </button>
            <div class="system-module-content">
                <div class="system-toolbar">
                    <button id="systemReleaseRefresh" type="button">刷新发布验证</button>
                    <span class="spacer"></span>
                    <button id="systemReleaseUpgrade" type="button" disabled>升级并分发</button>
                    <button id="systemReleaseRollback" type="button" disabled>一键回滚</button>
                </div>
                <div class="release-overview">
                    <div class="release-stat"><small>发布验证</small><strong id="systemReleaseCi">尚未加载</strong></div>
                    <div class="release-stat"><small>发布策略</small><strong id="systemReleasePolicy">尚未加载</strong></div>
                    <div class="release-stat"><small>版本</small><strong><span id="systemReleaseCurrent">-</span> → <span id="systemReleaseTarget">-</span></strong></div>
                </div>
                <div class="release-stat">
                    <small>集群实时进度 · 已收敛 <span id="systemReleaseConvergence">0 / 0</span></small>
                    <div class="release-progress-shell"><div id="systemReleaseProgressBar" class="release-progress-bar"></div></div>
                    <div id="systemReleaseProgressCaption" class="release-progress-caption">尚未开始</div>
                </div>
                <div id="systemReleaseSteps" class="release-steps"></div>
                <div id="systemReleaseNodes" class="release-node-list"></div>
                <pre id="systemReleaseError" class="release-error hidden"></pre>
            </div>
        `;
        const nodes = element('nodesPanel');
        (nodes?.parentElement || document.querySelector('.admin-console'))?.insertBefore(panel, nodes || null);
        panel.querySelector('.module-heading').onclick = () => {
            const open = setExpanded(panel);
            if (open) refresh(true).catch(showError);
            else if (!busy(lastValue?.local || {})) schedule(0);
        };
        element('systemReleaseRefresh').onclick = () => refresh(true).catch(showError);
        element('systemReleaseUpgrade').onclick = () => runAction(`${RELEASE_BASE}/upgrade`, '正在提交升级任务…');
        element('systemReleaseRollback').onclick = () => runAction(`${RELEASE_BASE}/rollback`, '正在提交回滚任务…');
    }

    init();
})();