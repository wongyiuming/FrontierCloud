(() => {
    'use strict';

    const DIRECTORY_MIN = -7;
    const DIRECTORY_MAX = 500000;

    function directoryCanMutate(path) {
        const parts = String(path || '').split('/').filter(Boolean);
        return parts.length >= 2 && parts.length <= 3 && ['music', 'vido'].includes(parts[0]);
    }

    function createRenameButton() {
        if (document.getElementById('renameDirectory')) return document.getElementById('renameDirectory');
        const renameButton = document.createElement('button');
        renameButton.id = 'renameDirectory';
        renameButton.type = 'button';
        renameButton.disabled = true;
        renameButton.textContent = '改名';
        renameButton.title = '重命名单个已选择文件夹';
        const deleteButton = document.getElementById('delete');
        if (deleteButton?.parentNode) deleteButton.parentNode.insertBefore(renameButton, deleteButton);
        return renameButton;
    }

    const renameButton = createRenameButton();
    const originalUpdateToolbar = updateToolbar;
    updateToolbar = function updateToolbarWithRename() {
        originalUpdateToolbar();
        const path = selected.size === 1 ? [...selected][0] : '';
        renameButton.disabled = !(
            selected.size === 1
            && selectionKind === 'directory'
            && directoryCanMutate(path)
        );
    };

    renameButton.onclick = async () => {
        if (renameButton.disabled) return;
        const path = [...selected][0];
        const currentName = path.split('/').pop() || '';
        const newName = prompt(`重命名文件夹「${currentName}」为：`, currentName);
        if (newName == null || newName.trim() === currentName) return;
        renameButton.disabled = true;
        try {
            const result = await api('/api/v1/media/admin/directory/rename', {
                method: 'POST',
                headers: requestHeaders(),
                body: JSON.stringify({path, new_name: newName.trim()}),
            });
            clearMediaSelection();
            await renderTree();
            updateToolbar();
            if (priorityScope === path || priorityScope.startsWith(`${path}/`)) {
                priorityScope = result.new_path + priorityScope.slice(path.length);
            }
        } catch (error) {
            alert(error.message);
            updateToolbar();
        }
    };

    function directoryPriorityRow(directory) {
        const path = String(directory.path || '');
        if (!directoryCanMutate(path)) {
            const rootButton = document.createElement('button');
            rootButton.type = 'button';
            rootButton.className = 'priority-directory';
            rootButton.innerHTML = `<span class="priority-directory-label"><strong>📁 ${expandableFilename(directory.name, 48)}</strong>`
                + `<small>/data/media/${expandableFilename(path, 68)}</small></span>`
                + `<span>${Number(directory.count || 0)} 个媒体&nbsp;&nbsp;进入</span>`;
            bindExpandableFilenames(rootButton);
            rootButton.onclick = () => {
                priorityScope = path;
                priorityPage = 1;
                $('prioritySearch').value = '';
                loadMediaPriority(true).catch(error => alert(error.message));
            };
            return rootButton;
        }

        const row = document.createElement('div');
        row.className = 'priority-directory priority-directory-row';
        row.dataset.priorityPath = path;
        row.dataset.priorityKind = 'directory';
        const enter = document.createElement('button');
        enter.type = 'button';
        enter.className = 'priority-directory-enter';
        enter.innerHTML = `<span class="priority-directory-label"><strong>📁 ${expandableFilename(directory.name, 48)}</strong>`
            + `<small>/data/media/${expandableFilename(path, 68)}</small></span>`
            + `<span>${Number(directory.count || 0)} 个媒体&nbsp;&nbsp;进入</span>`;
        bindExpandableFilenames(enter);
        enter.onclick = () => {
            priorityScope = path;
            priorityPage = 1;
            $('prioritySearch').value = '';
            loadMediaPriority(true).catch(error => alert(error.message));
        };

        const preference = Number(directory.preference || 0);
        const control = document.createElement('label');
        control.className = 'priority-control priority-directory-control';
        control.innerHTML = `<span>文件夹优先级 <output>${preference}</output></span>`
            + `<input type="range" min="${DIRECTORY_MIN}" max="${DIRECTORY_MAX}" value="${preference}" step="1" aria-label="${escapeHtml(directory.name)}的文件夹优先级">`;
        const slider = control.querySelector('input');
        const output = control.querySelector('output');
        let committed = preference;
        slider.oninput = () => { output.textContent = slider.value; };
        slider.onchange = async () => {
            const value = Number(slider.value);
            if (value === committed) return;
            slider.disabled = true;
            try {
                const result = await api('/api/v1/media/admin/directory-priority', {
                    method: 'POST',
                    headers: requestHeaders(),
                    body: JSON.stringify({path, value}),
                });
                committed = Number(result.preference);
                directory.preference = committed;
                slider.value = String(committed);
                output.textContent = String(committed);
                await loadMediaPriority(true);
            } catch (error) {
                slider.value = String(committed);
                output.textContent = String(committed);
                alert(error.message);
            } finally {
                slider.disabled = false;
            }
        };
        row.append(enter, control);
        attachPriorityNumber(control);
        return row;
    }

    priorityDirectoryButton = directoryPriorityRow;

    let loadVersion = 0;
    loadMediaPriority = async function loadMediaPriorityWithDirectories(force = false) {
        if (priorityLoading && !force) return;
        const version = ++loadVersion;
        priorityLoading = true;
        try {
            const params = new URLSearchParams({page: String(priorityPage), page_size: '100'});
            const query = $('prioritySearch').value.trim();
            const mediaType = $('priorityType').value;
            if (query) params.set('q', query);
            if (mediaType) params.set('media_type', mediaType);
            if (priorityScope) params.set('path', priorityScope);
            const data = await api(`/api/v1/media/admin/media-priority?${params}`);
            const orders = {};
            if (!data.searching && data.scope) {
                for (const [kind, field] of [['directory', 'directories'], ['media', 'items']]) {
                    if (!data[field]?.length) continue;
                    orders[kind] = await api(`/api/v1/media/admin/priority-order?scope=${encodeURIComponent(data.scope)}&kind=${kind}`);
                    const facts = new Map(orders[kind].items.map((item, index) => [item.path, {...item, index}]));
                    data[field] = data[field].filter(item => facts.has(item.path || item.media_path));
                    for (const item of data[field]) item.preference = facts.get(item.path || item.media_path).preference;
                    data[field].sort((a, b) => facts.get(a.path || a.media_path).index - facts.get(b.path || b.media_path).index);
                }
            }
            if (version !== loadVersion) return;
            window.FrontierPriorityDrag?.cancel();
            renderMediaPriority(data);
            window.FrontierPriorityDrag?.activate(data, orders);
            if (!data.searching) {
                $('prioritySummary').textContent = `${$('priorityPath').title || '/data/media'} 目录和媒体均可独立设置排序优先级`;
            }
        } finally {
            if (version === loadVersion) priorityLoading = false;
        }
    };

    updateToolbar();
})();
