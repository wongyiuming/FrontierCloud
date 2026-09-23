let selected = new Set();
let selectionKind = null;
let currentPath = '';
let csrfCookieName = '__Host-admin-csrf';
let uploadRunning = false;
let mediaSearchTimer = null;
let securityTimer = null;
let securityLoading = false;
let securityPage = 1;
let securityPages = 1;
let networkPage = 1;
let networkPages = 1;
let networkView = 'public';
const networkExpandedGroups = new Set();
let priorityPage = 1;
let priorityPages = 1;
let priorityLoading = false;
let priorityScope = '';
let lyricCatalog = null;
let lyricOrigin = null;
let lyricTargets = new Set();
let lyricTrackAnchor = null;
let lyricScopes = {track: 'music', lyric: 'lyrics'};
let lyricSearchTimer = null;
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
        if (module.dataset.adminModule === 'network') {
            loadNetworkObservations().catch(error => {
                $('networkSummary').textContent = `加载失败：${error.message}`;
            });
        }
        if (module.dataset.adminModule === 'priority') {
            loadMediaPriority().catch(error => {
                $('prioritySummary').textContent = `加载失败：${error.message}`;
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
    const rawQuery = $('mediaSearch').value.trim();
    const searching = Boolean(rawQuery);
    if (searching && !currentPath) {
        $('mediaSearch').value = '';
        alert('禁止在 data/media 执行全局搜索，请先进入 music、vido 或 lyrics');
    }
    const activeQuery = currentPath ? $('mediaSearch').value.trim() : '';
    const scopedSearch = Boolean(activeQuery);
    const endpoint = scopedSearch
        ? `/api/v1/media/admin/tree/search?q=${encodeURIComponent(activeQuery)}&path=${encodeURIComponent(currentPath)}`
        : `/api/v1/media/admin/tree?path=${encodeURIComponent(currentPath)}`;
    const data = await api(endpoint);
    $('mediaSearch').disabled = !currentPath;
    $('mediaSearch').placeholder = currentPath
        ? `仅搜索 /data/media/${currentPath} 及其子目录`
        : '请先进入 music、vido 或 lyrics 后搜索';
    $('pathbar').textContent = scopedSearch
        ? `/${currentPath} 内搜索：${activeQuery} · ${data.items.length}${data.truncated ? '+' : ''} 项`
        : `/${currentPath}`;
    const tree = $('tree');
    tree.innerHTML = '';

    if (currentPath && !scopedSearch) {
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
        const pathDetail = scopedSearch
            ? `<small class="tree-path">媒体路径 ${expandableFilename(`/${item.path.split('/').slice(1).join('/')}`, 72)}</small>`
            : '';
        row.innerHTML = `<span class="kind">${item.kind === 'directory' ? '📁' : '📄'}</span>`
            + `<span class="tree-label"><span class="name" title="${escapeHtml(item.name)}">${expandableFilename(item.name, 52)}</span>${pathDetail}</span>`
            + `<small>${item.kind === 'file' ? formatSize(item.size) : ''}</small>`;
        bindExpandableFilenames(row);
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

function lyricUsageMatches(item, kind) {
    const filter = $(kind === 'track' ? 'lyricsTrackUsage' : 'lyricsFileUsage').value;
    const used = kind === 'track' ? Boolean(item.lyric_path) : Number(item.linked_count || 0) > 0;
    return filter === 'all' || (filter === 'used' && used) || (filter === 'unused' && !used);
}

function visibleLyricItems(kind) {
    const source = kind === 'track' ? lyricCatalog?.tracks : lyricCatalog?.lyrics;
    return (source || []).filter(item => lyricUsageMatches(item, kind));
}

function renderLyricRelationJson() {
    $('lyricsRelationJson').textContent = JSON.stringify({
        '歌词': lyricOrigin?.path || null,
        '曲目': lyricOrigin ? [...lyricTargets].sort((left, right) => left.localeCompare(right, 'zh-Hans-CN')) : [],
    }, null, 2);
}

function exitLyricSelection(message = '请选择一份歌词') {
    lyricOrigin = null;
    lyricTargets.clear();
    lyricTrackAnchor = null;
    $('lyricsSaveLink').classList.add('hidden');
    $('lyricsModeStatus').textContent = message;
    renderLyricObjects();
    renderLyricRelationJson();
}

function activateLyric(path) {
    lyricOrigin = {kind: 'lyric', path};
    lyricTargets = lyricRelationSet('lyric', path);
    lyricTrackAnchor = null;
    $('lyricsSaveLink').classList.remove('hidden');
    $('lyricsModeStatus').textContent = `正在编辑：${path}；点击曲目添加或取消，Esc 退出`;
    renderLyricObjects();
    renderLyricRelationJson();
}

function toggleLyricTrack(path, event) {
    if (!lyricOrigin) return;
    const items = visibleLyricItems('track');
    const index = items.findIndex(item => item.path === path);
    if (event.shiftKey && lyricTrackAnchor !== null && index >= 0) {
        const start = Math.min(lyricTrackAnchor, index);
        const end = Math.max(lyricTrackAnchor, index);
        const shouldAdd = !lyricTargets.has(path);
        for (const item of items.slice(start, end + 1)) {
            if (shouldAdd) lyricTargets.add(item.path);
            else lyricTargets.delete(item.path);
        }
    } else if (lyricTargets.has(path)) {
        lyricTargets.delete(path);
    } else {
        lyricTargets.add(path);
    }
    if (index >= 0) lyricTrackAnchor = index;
    renderLyricObjects();
    renderLyricRelationJson();
}

function selectLyricObject(kind, path, event) {
    if (kind === 'lyric') {
        if (lyricOrigin?.path === path) exitLyricSelection();
        else activateLyric(path);
        return;
    }
    toggleLyricTrack(path, event);
}

function lyricObjectButton(item, kind) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'lyrics-object';
    const path = item.path;
    const isOrigin = lyricOrigin?.path === path;
    const isTarget = kind === 'track' && Boolean(lyricOrigin) && lyricTargets.has(path);
    const used = kind === 'track' ? Boolean(item.lyric_path) : Number(item.linked_count || 0) > 0;
    button.classList.toggle('selected', isOrigin);
    button.classList.toggle('linked', isTarget || (!lyricOrigin && used));
    button.disabled = kind === 'track' && !lyricOrigin;
    const count = kind === 'track' ? (item.lyric_path ? '已关联' : '未关联') : `${item.linked_count || 0} 首`;
    const displayPath = `/${path.split('/').slice(1).join('/')}`;
    button.innerHTML = `<span class="lyrics-object-label" title="${escapeHtml(path)}"><strong>${expandableFilename(item.name, 34)}</strong><small>媒体路径 ${expandableFilename(displayPath, 52)}</small></span><small>${count}</small>`;
    bindExpandableFilenames(button);
    button.onclick = event => {
        if (event.detail > 1) return;
        selectLyricObject(kind, path, event);
    };
    if (kind === 'lyric') {
        button.ondblclick = async event => {
            event.preventDefault();
            event.stopPropagation();
            if (lyricOrigin?.path !== path) activateLyric(path);
            const filename = path.split('/').pop() || item.name;
            $('lyricsTrackFilter').value = filename.replace(/\.lrc$/i, '');
            try {
                await loadLyricCatalog();
                lyricTargets = new Set((lyricCatalog.tracks || []).map(track => track.path));
                lyricTrackAnchor = lyricCatalog.tracks?.length ? lyricCatalog.tracks.length - 1 : null;
                $('lyricsModeStatus').textContent = `已选择歌词并全选 ${lyricTargets.size} 条搜索结果；请确认后保存`;
                renderLyricObjects();
                renderLyricRelationJson();
            } catch (error) {
                alert(error.message);
            }
        };
    }
    return button;
}

function lyricDirectoryButton(item, kind) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'lyrics-object lyrics-directory';
    button.innerHTML = `<span class="lyrics-object-label" title="${escapeHtml(item.path)}"><strong>📁 ${expandableFilename(item.name, 34)}</strong><small>/data/media/${expandableFilename(item.path, 52)}</small></span><small>进入</small>`;
    bindExpandableFilenames(button);
    button.onclick = () => {
        lyricScopes[kind] = item.path;
        const filter = kind === 'track' ? $('lyricsTrackFilter') : $('lyricsFileFilter');
        filter.value = '';
        lyricTrackAnchor = null;
        loadLyricCatalog().catch(error => alert(error.message));
    };
    return button;
}

function lyricUpButton(kind) {
    const root = kind === 'track' ? 'music' : 'lyrics';
    if (lyricScopes[kind] === root) return null;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'lyrics-object lyrics-directory';
    button.innerHTML = '<span class="lyrics-object-label"><strong>↩ 返回上级</strong></span>';
    button.onclick = () => {
        lyricScopes[kind] = lyricScopes[kind].split('/').slice(0, -1).join('/');
        const filter = kind === 'track' ? $('lyricsTrackFilter') : $('lyricsFileFilter');
        filter.value = '';
        lyricTrackAnchor = null;
        loadLyricCatalog().catch(error => alert(error.message));
    };
    return button;
}

function renderLyricObjects() {
    if (!lyricCatalog) return;
    const tracks = $('lyricsTrackList');
    const files = $('lyricsFileList');
    tracks.innerHTML = '';
    files.innerHTML = '';
    const trackSearching = Boolean($('lyricsTrackFilter').value.trim());
    const lyricSearching = Boolean($('lyricsFileFilter').value.trim());
    const trackUp = !trackSearching ? lyricUpButton('track') : null;
    const lyricUp = !lyricSearching ? lyricUpButton('lyric') : null;
    if (lyricUp) files.appendChild(lyricUp);
    if (trackUp) tracks.appendChild(trackUp);
    if (!lyricSearching) {
        for (const item of lyricCatalog.lyric_directories || []) files.appendChild(lyricDirectoryButton(item, 'lyric'));
    }
    if (!trackSearching) {
        for (const item of lyricCatalog.track_directories || []) tracks.appendChild(lyricDirectoryButton(item, 'track'));
    }
    for (const item of visibleLyricItems('lyric')) files.appendChild(lyricObjectButton(item, 'lyric'));
    for (const item of visibleLyricItems('track')) tracks.appendChild(lyricObjectButton(item, 'track'));
    if (!files.children.length) files.innerHTML = '<div class="lyrics-empty">没有符合条件的歌词</div>';
    if (!tracks.children.length) tracks.innerHTML = '<div class="lyrics-empty">没有符合条件的曲目</div>';
}

async function loadLyricCatalog() {
    const params = new URLSearchParams({
        track_path: lyricScopes.track,
        lyric_path: lyricScopes.lyric,
    });
    const trackQuery = $('lyricsTrackFilter').value.trim();
    const lyricQuery = $('lyricsFileFilter').value.trim();
    if (trackQuery) params.set('track_q', trackQuery);
    if (lyricQuery) params.set('lyric_q', lyricQuery);
    lyricCatalog = await api(`/api/v1/media/admin/lyrics/catalog?${params}`);
    lyricScopes = {...lyricScopes, ...lyricCatalog.scopes};
    const trackPathLabel = `/data/media/${lyricScopes.track}${trackQuery ? ` 内搜索：${trackQuery}${lyricCatalog.truncated.track ? '（仅显示前 200 项）' : ''}` : ''}`;
    const lyricPathLabel = `/data/media/${lyricScopes.lyric}${lyricQuery ? ` 内搜索：${lyricQuery}${lyricCatalog.truncated.lyric ? '（仅显示前 200 项）' : ''}` : ''}`;
    $('lyricsTrackPath').innerHTML = expandableFilename(trackPathLabel, 88);
    $('lyricsFilePath').innerHTML = expandableFilename(lyricPathLabel, 88);
    bindExpandableFilenames($('lyricsTrackPath'));
    bindExpandableFilenames($('lyricsFilePath'));
    $('lyricsTrackCount').textContent = String(lyricCatalog.counts.tracks);
    $('lyricsFileCount').textContent = String(lyricCatalog.counts.lyrics);
    $('lyricsRelationCount').textContent = String(lyricCatalog.counts.relations);
    if (lyricOrigin && !lyricCatalog.lyrics.some(item => item.path === lyricOrigin.path)) {
        exitLyricSelection('当前筛选中看不到原歌词，已退出选择状态');
        return;
    }
    renderLyricObjects();
    renderLyricRelationJson();
}

$('lyricsRefresh').onclick = () => loadLyricCatalog().catch(error => alert(error.message));
for (const id of ['lyricsTrackFilter', 'lyricsFileFilter']) {
    $(id).oninput = () => {
        clearTimeout(lyricSearchTimer);
        lyricTrackAnchor = null;
        lyricSearchTimer = setTimeout(() => loadLyricCatalog().catch(error => alert(error.message)), 220);
    };
}
for (const id of ['lyricsTrackUsage', 'lyricsFileUsage']) {
    $(id).onchange = () => {
        lyricTrackAnchor = null;
        renderLyricObjects();
    };
}
$('mediaSearch').oninput = () => {
    clearTimeout(mediaSearchTimer);
    selected.clear();
    selectionKind = null;
    mediaSearchTimer = setTimeout(() => renderTree().catch(error => alert(error.message)), 220);
};

globalThis.addEventListener?.('keydown', event => {
    if (event.key === 'Escape' && lyricOrigin) exitLyricSelection();
});

$('lyricsSaveLink').onclick = async () => {
    if (!lyricOrigin) return;
    $('lyricsSaveLink').disabled = true;
    try {
        await api('/api/v1/media/admin/lyrics/relations', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({
                origin_kind: 'lyric',
                origin_path: lyricOrigin.path,
                linked_paths: [...lyricTargets],
            }),
        });
        const savedPath = lyricOrigin.path;
        await loadLyricCatalog();
        if (lyricOrigin?.path === savedPath) lyricTargets = lyricRelationSet('lyric', savedPath);
        $('lyricsModeStatus').textContent = `已保存：${savedPath}；再次点击歌词或按 Esc 退出`;
        renderLyricObjects();
        renderLyricRelationJson();
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
        row.className = `security-row ${event.status || ''}${event.active ? ' active' : ''}${event.whitelisted ? ' whitelisted' : ''}`;
        const main = document.createElement('div');
        main.className = 'security-row-main';
        const ip = document.createElement('div');
        ip.className = 'security-ip';
        ip.textContent = event.ip;
        const meta = document.createElement('div');
        meta.className = 'security-meta';
        const statuses = {
            active: '当前封禁',
            observed: '攻击观察中（未封禁）',
            history: '历史封禁过',
            permanent: '已永久封禁',
        };
        const lastAttack = event.last_attack_at || '无攻击时间';
        meta.textContent = `${statuses[event.status] || event.status} · 攻击 ${event.attack_count || 0} 次 · 最近攻击 ${lastAttack} · 累计封禁 ${event.ban_count || 0} 次`;
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
        const lastAttack = entry.last_attack_at || '无攻击时间';
        meta.textContent = `白名单 · 攻击 ${entry.attack_count || 0} 次 · 最近攻击 ${lastAttack} · ${entry.note || '无备注'}`;
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
            sort_order: $('securityIpOrder').value,
            page: String(securityPage),
            page_size: '100',
        });
        const ip = $('securityIpFilter').value.trim();
        const status = $('securityStatusFilter').value;
        params.set('match_mode', $('securityMatchMode').value);
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

function updateIpSearchPlaceholders() {
    const securityFuzzy = $('securityMatchMode').value === 'fuzzy';
    $('securityIpFilter').placeholder = securityFuzzy ? 'IP 片段（至少 3 个字符）' : '精确 IP（可选）';
    const networkFuzzy = $('networkMatchMode').value === 'fuzzy';
    $('networkPublicIp').placeholder = networkFuzzy ? '公网 IP 片段（至少 3 字符）' : '公网 IP（精确查询）';
    $('networkWebrtcIp').placeholder = networkFuzzy ? 'WebRTC IP 片段（至少 3 字符）' : 'WebRTC IP（精确查询）';
}

function middleEllipsis(value, maximum = 42) {
    const characters = Array.from(String(value));
    if (characters.length <= maximum) return {text: characters.join(''), truncated: false};
    const head = Math.ceil((maximum - 3) / 2);
    const tail = Math.floor((maximum - 3) / 2);
    return {text: `${characters.slice(0, head).join('')}...${characters.slice(-tail).join('')}`, truncated: true};
}

function expandableFilename(value, maximum = 42) {
    const full = String(value);
    const display = middleEllipsis(full, maximum);
    if (!display.truncated) return `<span class="filename-full">${escapeHtml(full)}</span>`;
    return `<span class="filename-toggle" role="button" tabindex="0" aria-expanded="false" data-full="${escapeHtml(full)}" data-short="${escapeHtml(display.text)}" title="点击展开完整名称">${escapeHtml(display.text)}</span>`;
}

function bindExpandableFilenames(root) {
    for (const element of root.querySelectorAll('.filename-toggle')) {
        const toggle = event => {
            event.stopPropagation();
            const expanded = element.getAttribute('aria-expanded') === 'true';
            element.setAttribute('aria-expanded', String(!expanded));
            element.textContent = expanded ? element.dataset.short : element.dataset.full;
            element.title = expanded ? '点击展开完整名称' : '点击收起名称';
        };
        element.onclick = toggle;
        element.onkeydown = event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggle(event);
            }
        };
    }
}

$('securityMatchMode').onchange = updateIpSearchPlaceholders;
$('networkMatchMode').onchange = updateIpSearchPlaceholders;
updateIpSearchPlaceholders();
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

function networkTimeLine(item) {
    return `首次 ${escapeHtml(item.first_seen || '-')} · 最近 ${escapeHtml(item.last_seen || '-')}`;
}

function prioritySectionLabel(text, count) {
    const label = document.createElement('div');
    label.className = 'priority-section-label';
    label.innerHTML = `<strong>${expandableFilename(text, 68)}</strong><span>${Number(count || 0)}</span>`;
    bindExpandableFilenames(label);
    return label;
}

function priorityDirectoryButton(directory) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'priority-directory';
    button.innerHTML = `<span class="priority-directory-label"><strong>📁 ${expandableFilename(directory.name, 48)}</strong>`
        + `<small>/data/media/${expandableFilename(directory.path, 68)}</small></span>`
        + `<span>${Number(directory.count || 0)} 个媒体&nbsp;&nbsp;进入</span>`;
    bindExpandableFilenames(button);
    button.onclick = () => {
        priorityScope = directory.path;
        priorityPage = 1;
        $('prioritySearch').value = '';
        loadMediaPriority(true).catch(error => alert(error.message));
    };
    return button;
}

function priorityMediaRow(item, data) {
    const row = document.createElement('div');
    row.className = 'priority-row';
    const resourceId = item.resource_id || null;
    const fileName = item.media_path.split('/').pop() || item.title;
    const preference = Number(item.preference || 0);
    row.innerHTML = `
        <div class="priority-main">
            <strong>${expandableFilename(item.title, 48)}</strong>
            <small><span>${item.type === 'audio' ? '音乐' : '视频'}</span><code>${expandableFilename(fileName, 58)}${item.hidden ? ' · 已隐藏' : ''}</code></small>
        </div>
        <label class="priority-control">
            <span>展示优先级 <output>${preference}</output></span>
            <input type="range" min="${Number(data.minimum)}" max="${Number(data.maximum)}" value="${preference}" step="1" aria-label="${escapeHtml(item.title)}的展示优先级">
        </label>`;
    bindExpandableFilenames(row);
    const slider = row.querySelector('input[type="range"]');
    const output = row.querySelector('output');
    let committed = preference;
    slider.oninput = () => { output.textContent = slider.value; };
    slider.onchange = async () => {
        const value = Number(slider.value);
        if (value === committed) return;
        slider.disabled = true;
        try {
            const result = await api('/api/v1/media/admin/media-priority', {
                method: 'POST',
                headers: requestHeaders(),
                body: JSON.stringify({media_path: item.media_path, resource_id: resourceId, value}),
            });
            committed = Number(result.preference);
            item.preference = committed;
            slider.value = String(committed);
            output.textContent = String(committed);
        } catch (error) {
            slider.value = String(committed);
            output.textContent = String(committed);
            alert(error.message);
        } finally {
            slider.disabled = false;
        }
    };
    return row;
}

function renderMediaPriority(data) {
    const list = $('priorityList');
    list.innerHTML = '';
    priorityScope = data.scope || '';
    const currentPath = `/data/media${priorityScope ? `/${priorityScope}` : ''}`;
    $('priorityPath').innerHTML = expandableFilename(currentPath, 88);
    bindExpandableFilenames($('priorityPath'));
    $('priorityPath').title = currentPath;
    $('priorityUp').disabled = !priorityScope;
    if (data.searching) {
        const groups = new Map();
        for (const item of data.items || []) {
            const directory = item.media_path.split('/').slice(0, -1).join('/');
            if (!groups.has(directory)) groups.set(directory, []);
            groups.get(directory).push(item);
        }
        for (const directory of [...groups.keys()].sort((left, right) => left.localeCompare(right, 'zh-Hans-CN'))) {
            const items = groups.get(directory);
            list.appendChild(prioritySectionLabel(`📁 /data/media/${directory}`, items.length));
            for (const item of items) list.appendChild(priorityMediaRow(item, data));
        }
    } else {
        if (data.directories?.length) {
            list.appendChild(prioritySectionLabel('目录', data.directories.length));
            for (const directory of data.directories) list.appendChild(priorityDirectoryButton(directory));
        }
        if (data.items?.length) {
            list.appendChild(prioritySectionLabel('当前目录媒体', data.pagination?.total));
            for (const item of data.items) list.appendChild(priorityMediaRow(item, data));
        }
    }
    if (!list.children.length) list.innerHTML = '<div class="priority-empty">当前目录没有符合条件的媒体或子目录</div>';
    priorityPage = data.pagination?.page || 1;
    priorityPages = data.pagination?.pages || 1;
    $('prioritySummary').textContent = data.searching
        ? `在 ${currentPath} 中找到 ${data.pagination?.total || 0} 个本机与跨节点媒体，结果按所在目录分类`
        : `${currentPath} 及其子目录共 ${data.catalog_total || 0} 个本机与跨节点媒体；进入目录后调整优先级`;
    $('priorityPageInfo').textContent = `第 ${priorityPage} / ${priorityPages} 页`;
    $('priorityPrev').disabled = priorityPage <= 1;
    $('priorityNext').disabled = priorityPage >= priorityPages;
}

async function loadMediaPriority(force = false) {
    if (priorityLoading && !force) return;
    priorityLoading = true;
    try {
        const params = new URLSearchParams({page: String(priorityPage), page_size: '100'});
        const query = $('prioritySearch').value.trim();
        const mediaType = $('priorityType').value;
        if (query) params.set('q', query);
        if (mediaType) params.set('media_type', mediaType);
        if (priorityScope) params.set('path', priorityScope);
        renderMediaPriority(await api(`/api/v1/media/admin/media-priority?${params}`));
    } finally {
        priorityLoading = false;
    }
}

$('priorityFilterForm').onsubmit = event => {
    event.preventDefault();
    priorityPage = 1;
    loadMediaPriority(true).catch(error => alert(error.message));
};
$('priorityRefresh').onclick = () => loadMediaPriority(true).catch(error => alert(error.message));
$('priorityUp').onclick = () => {
    if (!priorityScope) return;
    priorityScope = priorityScope.includes('/') ? priorityScope.split('/').slice(0, -1).join('/') : '';
    priorityPage = 1;
    $('prioritySearch').value = '';
    loadMediaPriority(true).catch(error => alert(error.message));
};
$('priorityType').onchange = () => {
    priorityScope = $('priorityType').value === 'audio' ? 'music'
        : ($('priorityType').value === 'video' ? 'vido' : '');
    priorityPage = 1;
    $('prioritySearch').value = '';
    loadMediaPriority(true).catch(error => alert(error.message));
};
$('priorityPrev').onclick = () => {
    if (priorityPage > 1) {
        priorityPage -= 1;
        loadMediaPriority(true).catch(error => alert(error.message));
    }
};
$('priorityNext').onclick = () => {
    if (priorityPage < priorityPages) {
        priorityPage += 1;
        loadMediaPriority(true).catch(error => alert(error.message));
    }
};

function renderNetworkGroups(list, data) {
    const reverse = data.view === 'webrtc';
    for (const group of data.groups || []) {
        const stateKey = `${data.view}:${group.key}`;
        const card = document.createElement('section');
        card.className = 'network-group';
        card.dataset.networkGroup = stateKey;
        const header = document.createElement('button');
        header.type = 'button';
        header.className = 'network-group-header';
        const relationCount = Number(group.relation_count ?? group.relations?.length ?? 0);
        header.innerHTML = `<div><strong>${escapeHtml(group.key)}</strong><small>${networkTimeLine(group)}</small></div>`
            + `<span>${group.observation_count || 0} 次 · N=${relationCount}</span>`;
        const branches = document.createElement('div');
        branches.className = 'network-branches';
        for (const relation of group.relations || []) {
            const child = document.createElement('div');
            child.className = 'network-branch';
            const target = reverse ? relation.client_ip : (relation.webrtc_ip || '未获取');
            child.innerHTML = `<div><strong>${escapeHtml(target)}</strong><small>${networkTimeLine(relation)}</small></div>`
                + `<span>${relation.observation_count || 0} 次</span>`;
            branches.appendChild(child);
        }
        const setExpanded = expanded => {
            header.setAttribute('aria-expanded', String(expanded));
            branches.hidden = !expanded;
            if (expanded) networkExpandedGroups.add(stateKey);
            else networkExpandedGroups.delete(stateKey);
        };
        header.onclick = () => setExpanded(header.getAttribute('aria-expanded') !== 'true');
        card.append(header, branches);
        setExpanded(networkExpandedGroups.has(stateKey));
        list.appendChild(card);
    }
}

function renderNetworkObservations(data) {
    const list = $('networkList');
    list.innerHTML = '';
    renderNetworkGroups(list, data);
    if (!list.children.length) {
        list.innerHTML = '<div class="security-empty">没有符合条件的 WebRTC 关系记录</div>';
    }
    networkPage = data.pagination?.page || 1;
    networkPages = data.pagination?.pages || 1;
    const summaries = {
        public: `公网 IP ${data.pagination?.total || 0} 个；展开查看它对应的全部 WebRTC IP`,
        webrtc: `WebRTC IP ${data.pagination?.total || 0} 个；展开查看它对应的全部公网 IP`,
    };
    $('networkSummary').textContent = summaries[data.view] || summaries.public;
    $('networkPageInfo').textContent = `第 ${networkPage} / ${networkPages} 页`;
    $('networkPrev').disabled = networkPage <= 1;
    $('networkNext').disabled = networkPage >= networkPages;
}

async function loadNetworkObservations() {
    const params = new URLSearchParams({page: String(networkPage), page_size: '100', view: networkView});
    const publicIp = $('networkPublicIp').value.trim();
    const webrtcIp = $('networkWebrtcIp').value.trim();
    params.set('match_mode', $('networkMatchMode').value);
    if (publicIp) params.set('public_ip', publicIp);
    if (webrtcIp) params.set('webrtc_ip', webrtcIp);
    renderNetworkObservations(await api(`/api/v1/media/admin/network/observations?${params}`));
}

$('networkFilterForm').onsubmit = event => {
    event.preventDefault();
    networkPage = 1;
    loadNetworkObservations().catch(error => alert(error.message));
};

for (const button of document.querySelectorAll('[data-network-view]')) {
    button.onclick = () => {
        networkView = button.dataset.networkView;
        networkExpandedGroups.clear();
        networkPage = 1;
        for (const candidate of document.querySelectorAll('[data-network-view]')) {
            candidate.classList.toggle('selected', candidate === button);
        }
        loadNetworkObservations().catch(error => alert(error.message));
    };
}
$('networkExpandAll').onclick = () => {
    for (const card of document.querySelectorAll('[data-network-group]')) {
        const header = card.querySelector('.network-group-header');
        if (header?.getAttribute('aria-expanded') !== 'true') header?.click();
    }
};
$('networkCollapseAll').onclick = () => {
    for (const card of document.querySelectorAll('[data-network-group]')) {
        const header = card.querySelector('.network-group-header');
        if (header?.getAttribute('aria-expanded') === 'true') header?.click();
    }
};
$('networkReset').onclick = () => {
    $('networkFilterForm').reset();
    updateIpSearchPlaceholders();
    networkExpandedGroups.clear();
    networkPage = 1;
    loadNetworkObservations().catch(error => alert(error.message));
};
$('networkPrev').onclick = () => {
    if (networkPage > 1) {
        networkPage -= 1;
        loadNetworkObservations().catch(error => alert(error.message));
    }
};
$('networkNext').onclick = () => {
    if (networkPage < networkPages) {
        networkPage += 1;
        loadNetworkObservations().catch(error => alert(error.message));
    }
};

async function changeAdminKey(payload) {
    const data = await api('/api/v1/media/admin/key/rotate', {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(payload),
    });
    $('newKeyValue').textContent = data.admin_key;
    $('newKeyTitle').textContent = '请立即保存新的长期 Admin Key';
    $('newKeyDetail').textContent = '长期有效；本次轮换已使其他管理会话和未使用临时 Key 失效';
    $('copyKey').textContent = '复制';
    $('newKeyResult').classList.remove('hidden');
}

$('randomKey').onclick = async () => {
    if (!confirm('确认生成随机强 Key？其他已登录会话将立即失效。')) return;
    try { await changeAdminKey({mode: 'random'}); } catch (error) { alert(error.message); }
};
$('temporaryKeyForm').onsubmit = async event => {
    event.preventDefault();
    const minutes = Number($('temporaryKeyMinutes').value);
    try {
        const data = await api('/api/v1/media/admin/key/temporary', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({minutes}),
        });
        $('newKeyValue').textContent = data.admin_key;
        $('newKeyTitle').textContent = '一次性临时 Admin Key';
        $('newKeyDetail').textContent = `未使用时 ${data.minutes} 分钟后失效；首次登录立即作废，并建立 ${data.minutes} 分钟滑动会话`;
        $('copyKey').textContent = '复制';
        $('newKeyResult').classList.remove('hidden');
    } catch (error) { alert(error.message); }
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
$('dismissKey').onclick = () => $('newKeyResult').classList.add('hidden');

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
        if (status.credential_kind !== 'persistent') {
            for (const id of ['randomKey', 'customKey', 'customKeyConfirm', 'customKeySubmit', 'temporaryKeyMinutes', 'temporaryKey']) {
                $(id).disabled = true;
            }
            $('temporaryKey').textContent = '临时会话不可签发';
        }
        await renderTree();
        await loadSecurityStatus(true);
        securityTimer = setInterval(loadSecurityStatus, 15000);
    } catch (_error) {
        // api() handles expired sessions and navigation.
    }
})();
// Karaoke user controls reuse the existing Admin session and CSRF boundary.
(() => {
    const panel = document.getElementById('usersPanel');
    if (!panel) return;
    const $u = id => document.getElementById(id);
    let page = 1, pages = 1;
    const human = value => `${(Number(value || 0) / 1048576).toFixed(1)} MiB`;
    async function mutate(userId, action, quotaMib = null) {
        await api(`/api/v1/media/admin/users/${userId}`, {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({action, quota_mib: quotaMib}),
        });
        await load();
    }
    function action(label, callback) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.onclick = () => callback().catch(error => alert(error.message)); return button;
    }
    async function load() {
        const params = new URLSearchParams({page: String(page), page_size: '50'});
        const query = $u('usersQuery').value.trim(); if (query) params.set('q', query);
        const data = await api(`/api/v1/media/admin/users?${params}`);
        page = data.pagination.page; pages = data.pagination.pages;
        $u('usersSummary').textContent = `共 ${data.pagination.total} 个用户`;
        $u('usersPageInfo').textContent = `第 ${page} / ${pages} 页`;
        $u('usersPrev').disabled = page <= 1; $u('usersNext').disabled = page >= pages;
        const body = $u('usersList'); body.replaceChildren();
        for (const user of data.items) {
            const row = document.createElement('tr');
            for (const text of [user.username, user.status, `${human(user.used_bytes)} / ${human(user.quota_bytes)}`, user.storage_relationship_id || '未绑定']) {
                const cell = document.createElement('td'); cell.textContent = text; row.append(cell);
            }
            const operations = document.createElement('td');
            const quota = document.createElement('input'); quota.type = 'number'; quota.min = '1'; quota.value = String(Math.ceil(user.quota_bytes / 1048576)); quota.setAttribute('aria-label', `${user.username} 配额 MiB`);
            operations.append(quota, action('保存配额', () => mutate(user.user_id, 'quota', Number(quota.value))),
                action(user.status === 'banned' ? '解封' : '封禁', () => mutate(user.user_id, user.status === 'banned' ? 'unban' : 'ban')),
                action('删除', async () => { if (confirm(`删除 ${user.username} 及全部录音？`)) await mutate(user.user_id, 'delete'); }));
            row.append(operations); body.append(row);
        }
    }
    const heading = panel.querySelector?.('.module-heading');
    if (heading) heading.addEventListener('click', () => { if (panel.classList.contains('expanded')) load().catch(error => alert(error.message)); });
    $u('usersFilterForm').onsubmit = event => { event.preventDefault(); page = 1; load().catch(error => alert(error.message)); };
    $u('usersRefresh').onclick = () => load().catch(error => alert(error.message));
    $u('usersPrev').onclick = () => { if (page > 1) { page -= 1; load().catch(error => alert(error.message)); } };
    $u('usersNext').onclick = () => { if (page < pages) { page += 1; load().catch(error => alert(error.message)); } };
})();
