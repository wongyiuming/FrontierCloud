let selected = new Set();
let selectionKind = null;
let currentPath = '';
let csrfCookieName = '__Host-admin-csrf';
let uploadRunning = false;
let securityTimer = null;
let securityLoading = false;
let securityPage = 1;
let securityPages = 1;
let lyricCatalog = null;
let lyricOrigin = null;
let lyricLinking = false;
let lyricTargets = new Set();
let uploadLimits = {
    max_upload_file_size: 800 * 1024 * 1024,
    max_upload_task_files: 5000,
    max_lyric_file_size: 2 * 1024 * 1024,
};

const $ = id => document.getElementById(id);

function expandAdminModule(target) {
    const shouldExpand = !target.classList.contains('expanded');
    for (const module of document.querySelectorAll('.admin-module')) {
        const expanded = module === target && shouldExpand;
        module.classList.toggle('expanded', expanded);
        const heading = module.querySelector?.('.module-heading');
        if (heading) {
            heading.setAttribute('aria-expanded', String(expanded));
            const indicator = heading.querySelector('b');
            if (indicator) indicator.textContent = expanded ? '−' : '＋';
        }
    }
}

for (const module of document.querySelectorAll('.admin-module')) {
    const heading = module.querySelector('.module-heading');
    if (heading) heading.onclick = () => {
        expandAdminModule(module);
        if (module.dataset.adminModule === 'lyrics' && !lyricCatalog) {
            loadLyricCatalog().catch(error => {
                $('lyricsModeStatus').textContent = `加载失败：${error.message}`;
            });
        }
    };
}

function getCookie(name) {
    return document.cookie
        .split('; ')
        .find(value => value.startsWith(`${name}=`))
        ?.split('=')
        .slice(1)
        .join('=') || '';
}

function csrf() {
    return getCookie(csrfCookieName);
}

function requestHeaders(json = true) {
    const result = {'X-CSRF-Token': csrf()};
    if (json) result['Content-Type'] = 'application/json';
    return result;
}

function formatErrorDetail(detail) {
    if (!detail) return '操作失败';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(item => {
            if (typeof item === 'string') return item;
            const location = Array.isArray(item.loc) ? item.loc.join('.') : '';
            return `${location ? `${location}: ` : ''}${item.msg || JSON.stringify(item)}`;
        }).join('；');
    }
    if (typeof detail === 'object') return detail.message || JSON.stringify(detail);
    return String(detail);
}

async function api(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 401) {
        if (securityTimer) clearInterval(securityTimer);
        alert('特权模式已失效，请重新提权');
        location.href = '/api/v1/media';
        throw new Error('特权模式已失效');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(formatErrorDetail(data.detail));
    return data;
}

function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    }[character]));
}

function formatSize(value) {
    if (value == null) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
    }
    return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function showModal(title, body, onConfirm) {
    $('modalTitle').textContent = title;
    $('modalBody').textContent = body;
    $('modal').classList.remove('hidden');
    $('modalCancel').onclick = () => $('modal').classList.add('hidden');
    $('modalOk').onclick = async () => {
        try {
            await onConfirm();
            $('modal').classList.add('hidden');
        } catch (error) {
            alert(error.message);
        }
    };
}

function updateToolbar() {
    const count = selected.size;
    $('selection').textContent = count
        ? `已选择 ${count} 个${selectionKind === 'directory' ? '目录' : '文件'}`
        : '未选择';
    $('download').disabled = !count;
    $('delete').disabled = !count;
    const selectedRows = [...document.querySelectorAll('.tree-row.selected')];
    const canHide = count && selectionKind === 'directory'
        && selectedRows.length === count
        && selectedRows.every(row => row.dataset.hideable === 'true');
    $('hide').disabled = !canHide;
    if (count && selectionKind === 'directory') {
        const rows = [...document.querySelectorAll('.tree-row.selected')];
        $('hide').textContent = rows.every(row => row.dataset.hidden === 'true') ? '恢复' : '隐藏';
    } else {
        $('hide').textContent = '隐藏';
    }
}

function toggleSelection(item) {
    if (selectionKind && selectionKind !== item.kind) {
        selected.clear();
        selectionKind = null;
    }
    selectionKind = item.kind;
    if (selected.has(item.path)) selected.delete(item.path);
    else selected.add(item.path);
    if (!selected.size) selectionKind = null;
    for (const row of document.querySelectorAll('.tree-row[data-path]')) {
        row.classList.toggle('selected', selected.has(row.dataset.path));
    }
    updateToolbar();
}

async function renderTree() {
    const data = await api(`/api/v1/media/admin/tree?path=${encodeURIComponent(currentPath)}`);
    $('pathbar').textContent = `/${currentPath}`;
    const tree = $('tree');
    tree.innerHTML = '';

    if (currentPath) {
        const up = document.createElement('div');
        up.className = 'tree-row';
        up.innerHTML = '<span class="kind">↩</span><span class="name">返回上级</span>';
        up.onclick = () => {
            currentPath = currentPath.split('/').slice(0, -1).join('/');
            selected.clear();
            selectionKind = null;
            renderTree();
        };
        tree.appendChild(up);
    }

    for (const item of data.items) {
        const row = document.createElement('div');
        row.className = `tree-row${selected.has(item.path) ? ' selected' : ''}${item.hidden ? ' hidden-item' : ''}`;
        row.dataset.path = item.path;
        row.dataset.hidden = String(item.hidden);
        row.dataset.hideable = String(item.hideable === true);
        row.innerHTML = `<span class="kind">${item.kind === 'directory' ? '📁' : '📄'}</span>`
            + `<span class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>`
            + `<small>${item.kind === 'file' ? formatSize(item.size) : ''}</small>`;
        row.onclick = event => {
            event.stopPropagation();
            toggleSelection(item);
        };
        row.ondblclick = event => {
            event.stopPropagation();
            if (item.kind === 'directory') {
                currentPath = item.path;
                selected.clear();
                selectionKind = null;
                renderTree();
            }
        };
        tree.appendChild(row);
    }
    updateToolbar();
}

function setUploadControlsDisabled(disabled) {
    uploadRunning = disabled;
    $('uploadBtn').disabled = disabled;
    $('uploadFiles').disabled = disabled;
    $('uploadFolder').disabled = disabled;
    $('uploadLyrics').disabled = disabled;
    $('fileInput').disabled = disabled;
    $('folderInput').disabled = disabled;
    $('lyricsInput').disabled = disabled;
}

function setProgress(elementId, percentId, value) {
    const bounded = Math.max(0, Math.min(100, value));
    $(elementId).value = bounded;
    $(percentId).textContent = `${Math.round(bounded)}%`;
}

function addUploadResult(name, status, message) {
    const row = document.createElement('div');
    row.className = `upload-result ${status}`;
    row.textContent = `${status === 'ok' ? '✓' : '✗'} ${name}${message ? ` — ${message}` : ''}`;
    $('uploadResults').appendChild(row);
    $('uploadResults').scrollTop = $('uploadResults').scrollHeight;
}

function parseXhrData(xhr) {
    if (xhr.response && typeof xhr.response === 'object') return xhr.response;
    try {
        return JSON.parse(xhr.responseText || '{}');
    } catch (_error) {
        return {};
    }
}

function uploadOne(formData, onProgress, endpoint = '/api/v1/media/admin/upload/item') {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', endpoint);
        xhr.responseType = 'json';
        xhr.setRequestHeader('X-CSRF-Token', csrf());
        xhr.upload.onprogress = event => {
            if (event.lengthComputable) onProgress(event.loaded / event.total);
        };
        xhr.onerror = () => reject(new Error('网络连接中断'));
        xhr.onabort = () => reject(new Error('上传已取消'));
        xhr.onload = () => {
            const data = parseXhrData(xhr);
            if (xhr.status === 401) {
                reject(new Error('特权模式已失效，请重新提权'));
                location.href = '/api/v1/media';
                return;
            }
            if (xhr.status < 200 || xhr.status >= 300) {
                reject(new Error(formatErrorDetail(data.detail)));
                return;
            }
            resolve(data);
        };
        xhr.send(formData);
    });
}

async function runUploadTask(fileList, relativePaths = null, lyricUpload = false) {
    if (uploadRunning) return;
    const files = [...fileList];
    if (!files.length) return;
    if (files.length > uploadLimits.max_upload_task_files) {
        alert(`一次上传任务最多选择 ${uploadLimits.max_upload_task_files} 个文件`);
        return;
    }
    if (!lyricUpload && !relativePaths && !currentPath) {
        alert('上传文件前请先进入 data/media/music 或 data/media/vido 下的分类目录');
        return;
    }

    setUploadControlsDisabled(true);
    $('uploadProgress').classList.remove('hidden');
    $('uploadResults').innerHTML = '';
    $('uploadTaskTitle').textContent = lyricUpload ? '歌词上传任务' : (relativePaths ? '文件夹上传任务' : '多文件上传任务');
    setProgress('currentProgress', 'currentPercent', 0);
    setProgress('totalProgress', 'totalPercent', 0);

    const totalUnits = files.reduce((sum, file) => sum + Math.max(file.size, 1), 0);
    let completedUnits = 0;
    let successCount = 0;
    let failedCount = 0;

    try {
        for (let index = 0; index < files.length; index += 1) {
            const file = files[index];
            const displayName = relativePaths ? relativePaths[index] : file.name;
            const fileUnits = Math.max(file.size, 1);
            $('currentFileLabel').textContent = `当前：${displayName}`;
            $('totalTaskLabel').textContent = `任务总进度 ${index + 1}/${files.length}`;
            $('uploadSummary').textContent = `成功 ${successCount}，失败 ${failedCount}`;
            setProgress('currentProgress', 'currentPercent', 0);

            const fileLimit = lyricUpload ? uploadLimits.max_lyric_file_size : uploadLimits.max_upload_file_size;
            if (file.size > fileLimit) {
                failedCount += 1;
                completedUnits += fileUnits;
                addUploadResult(displayName, 'error', `超过 ${formatSize(fileLimit)} 限制`);
                setProgress('totalProgress', 'totalPercent', completedUnits / totalUnits * 100);
                continue;
            }

            const formData = new FormData();
            if (!lyricUpload) formData.append('target_dir', currentPath);
            if (!lyricUpload && relativePaths) formData.append('relative_path', relativePaths[index]);
            formData.append('file', file, file.name);

            try {
                const result = await uploadOne(formData, fraction => {
                    setProgress('currentProgress', 'currentPercent', fraction * 100);
                    setProgress(
                        'totalProgress',
                        'totalPercent',
                        (completedUnits + fileUnits * fraction) / totalUnits * 100,
                    );
                }, lyricUpload ? '/api/v1/media/admin/upload/lyric' : '/api/v1/media/admin/upload/item');
                successCount += 1;
                addUploadResult(displayName, 'ok', result.path);
            } catch (error) {
                failedCount += 1;
                addUploadResult(displayName, 'error', error.message);
            }

            completedUnits += fileUnits;
            setProgress('currentProgress', 'currentPercent', 100);
            setProgress('totalProgress', 'totalPercent', completedUnits / totalUnits * 100);
        }
    } finally {
        setUploadControlsDisabled(false);
        $('uploadSummary').textContent = `完成：成功 ${successCount}，失败 ${failedCount}`;
        $('currentFileLabel').textContent = '当前文件处理完成';
        await renderTree().catch(() => {});
        if (lyricUpload) await loadLyricCatalog().catch(() => {});
    }
}

function lyricRelationSet(kind, path) {
    if (!lyricCatalog) return new Set();
    return new Set(lyricCatalog.relations
        .filter(relation => kind === 'track' ? relation.track === path : relation.lyric === path)
        .map(relation => kind === 'track' ? relation.lyric : relation.track));
}

function selectLyricObject(kind, path) {
    if (!lyricLinking) {
        lyricOrigin = {kind, path};
        $('lyricsEnterLink').disabled = false;
        $('lyricsModeStatus').textContent = `已选择${kind === 'track' ? '曲目' : '歌词'}：${path}`;
    } else if (lyricOrigin && lyricOrigin.kind !== kind) {
        if (lyricOrigin.kind === 'track') {
            lyricTargets = lyricTargets.has(path) ? new Set() : new Set([path]);
        } else if (lyricTargets.has(path)) {
            lyricTargets.delete(path);
        } else {
            lyricTargets.add(path);
        }
    }
    renderLyricObjects();
    renderLyricGraph();
}

function lyricObjectButton(item, kind) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'lyrics-object';
    const path = item.path;
    const isOrigin = lyricOrigin?.kind === kind && lyricOrigin.path === path;
    const isTarget = lyricLinking && lyricOrigin?.kind !== kind && lyricTargets.has(path);
    button.classList.toggle('selected', isOrigin);
    button.classList.toggle('linked', isTarget || (!lyricLinking && (kind === 'track' ? item.lyric_path : item.linked_count)));
    button.disabled = lyricLinking && lyricOrigin?.kind === kind && !isOrigin;
    const count = kind === 'track' ? (item.lyric_path ? '已关联' : '未关联') : `${item.linked_count || 0} 首`;
    button.innerHTML = `<span title="${escapeHtml(path)}">${escapeHtml(item.name)}</span><small>${count}</small>`;
    button.onclick = () => selectLyricObject(kind, path);
    return button;
}

function renderLyricObjects() {
    if (!lyricCatalog) return;
    const trackNeedle = $('lyricsTrackFilter').value.trim().toLocaleLowerCase();
    const lyricNeedle = $('lyricsFileFilter').value.trim().toLocaleLowerCase();
    const tracks = $('lyricsTrackList');
    const files = $('lyricsFileList');
    tracks.innerHTML = '';
    files.innerHTML = '';
    for (const item of lyricCatalog.tracks.filter(item => `${item.name} ${item.path}`.toLocaleLowerCase().includes(trackNeedle))) {
        tracks.appendChild(lyricObjectButton(item, 'track'));
    }
    for (const item of lyricCatalog.lyrics.filter(item => `${item.name} ${item.path}`.toLocaleLowerCase().includes(lyricNeedle))) {
        files.appendChild(lyricObjectButton(item, 'lyric'));
    }
}

function svgLabel(path) {
    const name = path.split('/').pop() || path;
    return name.length > 28 ? `${name.slice(0, 27)}…` : name;
}

function renderLyricGraph() {
    const graph = $('lyricsGraph');
    if (!lyricOrigin) {
        graph.setAttribute('viewBox', '0 0 900 180');
        graph.innerHTML = '<text x="450" y="94" text-anchor="middle">选择一个对象后，仅展示与它相关的连线</text>';
        return;
    }
    const targets = lyricLinking ? [...lyricTargets] : [...lyricRelationSet(lyricOrigin.kind, lyricOrigin.path)];
    const height = Math.max(180, targets.length * 58 + 36);
    const originX = lyricOrigin.kind === 'track' ? 70 : 610;
    const targetX = lyricOrigin.kind === 'track' ? 610 : 70;
    const originY = height / 2 - 20;
    let markup = `<rect class="node" x="${originX}" y="${originY}" width="220" height="40" rx="8"/>`
        + `<text x="${originX + 110}" y="${originY + 25}" text-anchor="middle">${escapeHtml(svgLabel(lyricOrigin.path))}</text>`;
    targets.forEach((path, index) => {
        const y = 18 + index * 58;
        const x1 = lyricOrigin.kind === 'track' ? originX + 220 : originX;
        const x2 = lyricOrigin.kind === 'track' ? targetX : targetX + 220;
        markup += `<line class="edge" x1="${x1}" y1="${originY + 20}" x2="${x2}" y2="${y + 20}"/>`
            + `<rect class="node target" x="${targetX}" y="${y}" width="220" height="40" rx="8"/>`
            + `<text x="${targetX + 110}" y="${y + 25}" text-anchor="middle">${escapeHtml(svgLabel(path))}</text>`;
    });
    if (!targets.length) markup += '<text x="450" y="145" text-anchor="middle">当前没有关联；进入连线后从另一列选择对象</text>';
    graph.setAttribute('viewBox', `0 0 900 ${height}`);
    graph.innerHTML = markup;
}

async function loadLyricCatalog() {
    lyricCatalog = await api('/api/v1/media/admin/lyrics/catalog');
    $('lyricsTrackCount').textContent = String(lyricCatalog.counts.tracks);
    $('lyricsFileCount').textContent = String(lyricCatalog.counts.lyrics);
    $('lyricsRelationCount').textContent = String(lyricCatalog.counts.relations);
    if (lyricOrigin) {
        const source = lyricOrigin.kind === 'track' ? lyricCatalog.tracks : lyricCatalog.lyrics;
        if (!source.some(item => item.path === lyricOrigin.path)) lyricOrigin = null;
    }
    renderLyricObjects();
    renderLyricGraph();
}

$('lyricsRefresh').onclick = () => loadLyricCatalog().catch(error => alert(error.message));
$('lyricsTrackFilter').oninput = renderLyricObjects;
$('lyricsFileFilter').oninput = renderLyricObjects;
$('lyricsEnterLink').onclick = () => {
    if (!lyricOrigin) return;
    lyricLinking = true;
    lyricTargets = lyricRelationSet(lyricOrigin.kind, lyricOrigin.path);
    $('lyricsEnterLink').classList.add('hidden');
    $('lyricsCancelLink').classList.remove('hidden');
    $('lyricsSaveLink').classList.remove('hidden');
    $('lyricsModeStatus').textContent = '连线编辑中：点击另一列对象添加或移除连线';
    renderLyricObjects();
    renderLyricGraph();
};
$('lyricsCancelLink').onclick = () => {
    lyricLinking = false;
    lyricTargets.clear();
    $('lyricsEnterLink').classList.remove('hidden');
    $('lyricsCancelLink').classList.add('hidden');
    $('lyricsSaveLink').classList.add('hidden');
    $('lyricsModeStatus').textContent = `已选择：${lyricOrigin?.path || ''}`;
    renderLyricObjects();
    renderLyricGraph();
};
$('lyricsSaveLink').onclick = async () => {
    if (!lyricOrigin) return;
    $('lyricsSaveLink').disabled = true;
    try {
        await api('/api/v1/media/admin/lyrics/relations', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({
                origin_kind: lyricOrigin.kind,
                origin_path: lyricOrigin.path,
                linked_paths: [...lyricTargets],
            }),
        });
        lyricLinking = false;
        lyricTargets.clear();
        $('lyricsEnterLink').classList.remove('hidden');
        $('lyricsCancelLink').classList.add('hidden');
        $('lyricsSaveLink').classList.add('hidden');
        $('lyricsModeStatus').textContent = `已保存：${lyricOrigin.path}`;
        await loadLyricCatalog();
    } catch (error) {
        alert(error.message);
    } finally {
        $('lyricsSaveLink').disabled = false;
    }
};

function securityButton(label, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    if (className) button.className = className;
    button.onclick = async () => {
        button.disabled = true;
        try {
            await handler();
            await loadSecurityStatus(true);
        } catch (error) {
            alert(error.message);
        } finally {
            button.disabled = false;
        }
    };
    return button;
}

function renderSecurityList(data) {
    $('legalApiCount').textContent = String(data.legal_api_count ?? 0);
    $('activeBanCount').textContent = String(data.active_ban_count ?? 0);
    $('whitelistCount').textContent = String(data.whitelist_count ?? 0);
    $('securitySummary').textContent = `首次超过阈值封禁 24 小时；第二次触犯永久封禁`;

    const banList = $('banList');
    banList.innerHTML = '';
    if (!data.events?.length) {
        const empty = document.createElement('div');
        empty.className = 'security-empty';
        empty.textContent = '当前页没有非白名单 IP';
        banList.appendChild(empty);
    }
    for (const event of data.events || []) {
        const row = document.createElement('div');
        row.className = `security-row${event.active ? ' active' : ''}${event.whitelisted ? ' whitelisted' : ''}`;
        const main = document.createElement('div');
        main.className = 'security-row-main';
        const ip = document.createElement('div');
        ip.className = 'security-ip';
        ip.textContent = event.ip;
        const meta = document.createElement('div');
        meta.className = 'security-meta';
        const statuses = {active: '封禁中', expired: '已到期', unbanned: '已解封', observed: '已记录'};
        const kind = event.active ? (event.ban_kind === 'permanent' ? ' · 永久' : ' · 临时') : '';
        meta.textContent = `${statuses[event.status] || event.status}${kind} · 累计封禁 ${event.ban_count || 0} 次`;
        const path = document.createElement('div');
        path.className = 'security-path';
        path.textContent = event.reason || '';
        main.append(ip, meta, path);
        const actions = document.createElement('div');
        actions.className = 'security-actions';
        if (event.active) {
            actions.appendChild(securityButton('解封', 'danger', () => api('/api/v1/media/admin/security/unban', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: event.ip}),
            })));
        }
        if (!event.whitelisted) {
            actions.appendChild(securityButton('加白', 'allow', () => api('/api/v1/media/admin/security/whitelist', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: event.ip, note: 'Admin 封禁列表加白'}),
            })));
            if (!event.active) {
                actions.appendChild(securityButton('重新封禁', 'danger', async () => {
                    const reason = prompt('请输入重新封禁原因:');
                    if (!reason?.trim()) return;
                    await api('/api/v1/media/admin/security/reban', {
                        method: 'POST',
                        headers: requestHeaders(),
                        body: JSON.stringify({ip: event.ip, reason: reason.trim()}),
                    });
                }));
            }
            if (!event.active || event.ban_kind !== 'permanent') {
                actions.appendChild(securityButton('永久拉黑', 'danger', async () => {
                    const reason = prompt(`请输入永久拉黑 ${event.ip} 的原因:`);
                    if (!reason?.trim()) return;
                    if (!confirm(`确认永久拉黑 ${event.ip}？该封禁不会自动到期。`)) return;
                    await api('/api/v1/media/admin/security/permanent-ban', {
                        method: 'POST',
                        headers: requestHeaders(),
                        body: JSON.stringify({ip: event.ip, reason: reason.trim()}),
                    });
                }));
            }
        }
        row.append(main, actions);
        banList.appendChild(row);
    }

    const whitelistList = $('whitelistList');
    whitelistList.innerHTML = '';
    if (!data.whitelist?.length) {
        const empty = document.createElement('div');
        empty.className = 'security-empty';
        empty.textContent = '当前页没有白名单 IP';
        whitelistList.appendChild(empty);
    }
    for (const entry of data.whitelist || []) {
        const row = document.createElement('div');
        row.className = 'security-row whitelisted';
        const main = document.createElement('div');
        main.className = 'security-row-main';
        const ip = document.createElement('div');
        ip.className = 'security-ip';
        ip.textContent = entry.ip;
        const meta = document.createElement('div');
        meta.className = 'security-meta';
        meta.textContent = entry.note || '永久白名单';
        main.append(ip, meta);
        const actions = document.createElement('div');
        actions.className = 'security-actions';
        actions.appendChild(securityButton('移出', 'danger', () => api('/api/v1/media/admin/security/whitelist/remove', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: entry.ip}),
        })));
        row.append(main, actions);
        whitelistList.appendChild(row);
    }

    securityPage = data.pagination?.page || 1;
    securityPages = data.pagination?.pages || 1;
    $('securityPageInfo').textContent = `第 ${securityPage} / ${securityPages} 页，共 ${data.pagination?.total || 0} 个 IP（含白名单）`;
    $('securityPrev').disabled = securityPage <= 1;
    $('securityNext').disabled = securityPage >= securityPages;
}

async function loadSecurityStatus(force = false) {
    if (securityLoading || (document.hidden && !force)) return;
    securityLoading = true;
    try {
        const params = new URLSearchParams({
            ip_order: $('securityIpOrder').value,
            page: String(securityPage),
            page_size: '100',
        });
        const ip = $('securityIpFilter').value.trim();
        const status = $('securityStatusFilter').value;
        if (ip) params.set('ip', ip);
        if (status) params.set('status', status);
        renderSecurityList(await api(`/api/v1/media/admin/security/blocks?${params}`));
    } catch (error) {
        $('securitySummary').textContent = `加载失败：${error.message}`;
    } finally {
        securityLoading = false;
    }
}

$('securityRefresh').onclick = () => loadSecurityStatus(true);
$('securityFilterForm').onsubmit = event => {
    event.preventDefault();
    securityPage = 1;
    loadSecurityStatus(true);
};
$('securityPrev').onclick = () => {
    if (securityPage > 1) {
        securityPage -= 1;
        loadSecurityStatus(true);
    }
};
$('securityNext').onclick = () => {
    if (securityPage < securityPages) {
        securityPage += 1;
        loadSecurityStatus(true);
    }
};
$('whitelistForm').onsubmit = async event => {
    event.preventDefault();
    const ip = $('whitelistIp').value.trim();
    if (!ip) return;
    try {
        await api('/api/v1/media/admin/security/whitelist', {
            method: 'POST',
            headers: requestHeaders(),
            body: JSON.stringify({ip, note: $('whitelistNote').value.trim()}),
        });
        $('whitelistIp').value = '';
        $('whitelistNote').value = '';
        await loadSecurityStatus(true);
    } catch (error) {
        alert(error.message);
    }
};
$('permanentBanForm').onsubmit = async event => {
    event.preventDefault();
    const ip = $('permanentBanIp').value.trim();
    const reason = $('permanentBanReason').value.trim();
    if (!ip || !reason) return;
    if (!confirm(`确认永久拉黑 ${ip}？该封禁不会自动到期。`)) return;
    try {
        await api('/api/v1/media/admin/security/permanent-ban', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip, reason}),
        });
        $('permanentBanForm').reset();
        await loadSecurityStatus(true);
    } catch (error) {
        alert(error.message);
    }
};

async function changeAdminKey(payload) {
    const data = await api('/api/v1/media/admin/key/rotate', {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(payload),
    });
    $('newKeyValue').textContent = data.admin_key;
    $('newKeyResult').classList.remove('hidden');
}

$('randomKey').onclick = async () => {
    if (!confirm('确认生成随机强 Key？其他已登录会话将立即失效。')) return;
    try { await changeAdminKey({mode: 'random'}); } catch (error) { alert(error.message); }
};
$('customKeyForm').onsubmit = async event => {
    event.preventDefault();
    const key = $('customKey').value;
    const confirmation = $('customKeyConfirm').value;
    if (key !== confirmation) { alert('两次输入的 Admin Key 不一致'); return; }
    if (!confirm('确认使用这个自定义 Key？其他已登录会话将立即失效。')) return;
    try {
        await changeAdminKey({mode: 'custom', key, confirmation});
        $('customKeyForm').reset();
    } catch (error) { alert(error.message); }
};
$('copyKey').onclick = async () => {
    await navigator.clipboard.writeText($('newKeyValue').textContent);
    $('copyKey').textContent = '已复制';
};

$('uploadFiles').onclick = () => $('fileInput').click();
$('uploadFolder').onclick = () => $('folderInput').click();
$('uploadLyrics').onclick = () => $('lyricsInput').click();
$('fileInput').onchange = async event => {
    await runUploadTask(event.target.files);
    event.target.value = '';
};
$('folderInput').onchange = async event => {
    const paths = [...event.target.files].map(file => file.webkitRelativePath || file.name);
    await runUploadTask(event.target.files, paths);
    event.target.value = '';
};
$('lyricsInput').onchange = async event => {
    await runUploadTask(event.target.files, null, true);
    event.target.value = '';
};

$('delete').onclick = () => {
    const paths = [...selected];
    showModal(
        '确认删除',
        `将删除选中的 ${paths.length} 个${selectionKind === 'directory' ? '目录及其全部内容' : '文件'}。此操作不可恢复。`,
        async () => {
            await api('/api/v1/media/admin/delete', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({paths}),
            });
            selected.clear();
            selectionKind = null;
            await renderTree();
        },
    );
};

$('hide').onclick = () => {
    const paths = [...selected];
    const hidden = $('hide').textContent === '隐藏';
    showModal(
        hidden ? '确认隐藏' : '确认恢复',
        `${hidden ? '公共视图将隐藏' : '公共视图将恢复显示'}选中的 ${paths.length} 个目录。`,
        async () => {
            await api('/api/v1/media/admin/hide', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({paths, hidden}),
            });
            selected.clear();
            selectionKind = null;
            await renderTree();
        },
    );
};

$('download').onclick = async () => {
    const paths = [...selected];
    const url = `/api/v1/media/admin/download?paths=${encodeURIComponent(JSON.stringify(paths))}`;
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = paths.length === 1
        ? paths[0].split('/').pop()
        : 'media-download.zip';
    anchor.click();
};

$('backPublic').onclick = () => { location.href = '/api/v1/media'; };
$('logout').onclick = async () => {
    try {
        await fetch('/api/v1/media/admin/logout', {method: 'POST', headers: requestHeaders(false)});
    } finally {
        location.href = '/api/v1/media';
    }
};

(async () => {
    try {
        const status = await api('/api/v1/media/admin/status');
        uploadLimits = {...uploadLimits, ...status.limits};
        csrfCookieName = status.csrf_cookie_name || csrfCookieName;
        await renderTree();
        await loadSecurityStatus(true);
        securityTimer = setInterval(loadSecurityStatus, 15000);
    } catch (_error) {
        // api() handles expired sessions and navigation.
    }
})();
