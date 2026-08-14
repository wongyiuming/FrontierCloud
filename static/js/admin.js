let selected = new Set();
let selectionKind = null;
let currentPath = '';
let csrfCookieName = '__Host-admin-csrf';
let uploadRunning = false;
let lastLogId = 0;
let logPolling = false;
let logTimer = null;
let securityTimer = null;
let securityLoading = false;
let uploadLimits = {
    max_upload_file_size: 800 * 1024 * 1024,
    max_upload_task_files: 5000,
};

const $ = id => document.getElementById(id);

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
        if (logTimer) clearInterval(logTimer);
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
    $('modalBody').innerHTML = body;
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
    $('hide').disabled = !(count && selectionKind === 'directory');
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
    $('fileInput').disabled = disabled;
    $('folderInput').disabled = disabled;
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

function uploadOne(formData, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/v1/media/admin/upload/item');
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

async function runUploadTask(fileList, relativePaths = null) {
    if (uploadRunning) return;
    const files = [...fileList];
    if (!files.length) return;
    if (files.length > uploadLimits.max_upload_task_files) {
        alert(`一次上传任务最多选择 ${uploadLimits.max_upload_task_files} 个文件`);
        return;
    }
    if (!relativePaths && !currentPath) {
        alert('上传文件前请先进入 data/media 下的子目录');
        return;
    }

    setUploadControlsDisabled(true);
    $('uploadProgress').classList.remove('hidden');
    $('uploadResults').innerHTML = '';
    $('uploadTaskTitle').textContent = relativePaths ? '文件夹上传任务' : '多文件上传任务';
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

            if (file.size > uploadLimits.max_upload_file_size) {
                failedCount += 1;
                completedUnits += fileUnits;
                addUploadResult(displayName, 'error', `超过 ${formatSize(uploadLimits.max_upload_file_size)} 限制`);
                setProgress('totalProgress', 'totalPercent', completedUnits / totalUnits * 100);
                continue;
            }

            const formData = new FormData();
            formData.append('target_dir', currentPath);
            if (relativePaths) formData.append('relative_path', relativePaths[index]);
            formData.append('file', file, file.name);

            try {
                const result = await uploadOne(formData, fraction => {
                    setProgress('currentProgress', 'currentPercent', fraction * 100);
                    setProgress(
                        'totalProgress',
                        'totalPercent',
                        (completedUnits + fileUnits * fraction) / totalUnits * 100,
                    );
                });
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
    }
}

async function pollLogs() {
    if (logPolling || document.hidden) return;
    logPolling = true;
    try {
        const data = await api(`/api/v1/media/admin/logs?after=${lastLogId}&limit=200`);
        const output = $('logOutput');
        const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
        if (lastLogId === 0) output.textContent = '';
        for (const entry of data.entries) {
            output.textContent += `${entry.timestamp} ${entry.line}\n`;
            lastLogId = Math.max(lastLogId, entry.id);
        }
        const lines = output.textContent.split('\n');
        if (lines.length > 501) output.textContent = lines.slice(-501).join('\n');
        if (nearBottom) output.scrollTop = output.scrollHeight;

        const badge = $('transportBadge');
        badge.textContent = location.protocol === 'https:' ? 'HTTPS 加密传输' : '本机回环调试';
        badge.classList.toggle('secure', data.secure_transport);
    } catch (error) {
        $('logOutput').textContent += `日志读取失败：${error.message}\n`;
    } finally {
        logPolling = false;
    }
}

function securityDate(value) {
    if (!value) return '-';
    const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

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
    $('whitelistCount').textContent = String(data.whitelist?.length ?? 0);
    $('securitySummary').textContent = `1 小时内超过 ${data.threshold} 次非法 API，封禁 ${Math.round(data.ban_seconds / 3600)} 小时`;

    const banList = $('banList');
    banList.innerHTML = '';
    if (!data.events?.length) {
        const empty = document.createElement('div');
        empty.className = 'security-empty';
        empty.textContent = '最近 24 小时没有自动封禁记录';
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
        meta.textContent = `${event.status} · ${event.trigger_count} 次 · ${securityDate(event.banned_at)} → ${securityDate(event.expires_at)}`;
        const path = document.createElement('div');
        path.className = 'security-path';
        path.textContent = `${event.last_method || ''} ${event.last_path || ''}`.trim();
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
        }
        row.append(main, actions);
        banList.appendChild(row);
    }

    const whitelistList = $('whitelistList');
    whitelistList.innerHTML = '';
    if (!data.whitelist?.length) {
        const empty = document.createElement('div');
        empty.className = 'security-empty';
        empty.textContent = '永久白名单为空';
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
        meta.textContent = `${securityDate(entry.created_at)}${entry.note ? ` · ${entry.note}` : ''}`;
        main.append(ip, meta);
        const actions = document.createElement('div');
        actions.className = 'security-actions';
        actions.appendChild(securityButton('移出', 'danger', () => api('/api/v1/media/admin/security/whitelist/remove', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: entry.ip}),
        })));
        row.append(main, actions);
        whitelistList.appendChild(row);
    }
}

async function loadSecurityStatus(force = false) {
    if (securityLoading || (document.hidden && !force)) return;
    securityLoading = true;
    try {
        renderSecurityList(await api('/api/v1/media/admin/security/blocks'));
    } catch (error) {
        $('securitySummary').textContent = `加载失败：${error.message}`;
    } finally {
        securityLoading = false;
    }
}

$('securityRefresh').onclick = () => loadSecurityStatus(true);
$('securityToggle').onclick = () => {
    const collapsed = $('securityPanel').classList.toggle('collapsed');
    $('securityToggle').textContent = collapsed ? '展开' : '收起';
    $('securityToggle').setAttribute('aria-expanded', String(!collapsed));
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

$('uploadFiles').onclick = () => $('fileInput').click();
$('uploadFolder').onclick = () => $('folderInput').click();
$('fileInput').onchange = async event => {
    await runUploadTask(event.target.files);
    event.target.value = '';
};
$('folderInput').onchange = async event => {
    const paths = [...event.target.files].map(file => file.webkitRelativePath || file.name);
    await runUploadTask(event.target.files, paths);
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
    const response = await fetch(url);
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        alert(formatErrorDetail(data.detail) || '下载失败');
        return;
    }
    const blob = await response.blob();
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = paths.length === 1 ? paths[0].split('/').pop() : 'media-download.zip';
    anchor.click();
    URL.revokeObjectURL(anchor.href);
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
        await pollLogs();
        await loadSecurityStatus(true);
        logTimer = setInterval(pollLogs, 2000);
        securityTimer = setInterval(loadSecurityStatus, 15000);
    } catch (_error) {
        // api() handles expired sessions and navigation.
    }
})();
