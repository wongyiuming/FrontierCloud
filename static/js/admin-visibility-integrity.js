'use strict';

const directlyHiddenPaths = new Set();
const inheritedHiddenPaths = new Set();

const visibilityApi = api;
api = async function apiWithVisibilityInheritance(url, options = {}) {
    const data = await visibilityApi(url, options);
    if (String(url).startsWith('/api/v1/media/admin/tree') && Array.isArray(data?.items)) {
        directlyHiddenPaths.clear();
        inheritedHiddenPaths.clear();
        for (const item of data.items) {
            if (item.hidden_direct === true) directlyHiddenPaths.add(item.path);
            if (item.hidden === true && item.hidden_direct !== true) inheritedHiddenPaths.add(item.path);
        }
    }
    return data;
};

const visibilityRenderTree = renderTree;
renderTree = async function renderTreeWithVisibilityInheritance() {
    await visibilityRenderTree();
    for (const row of document.querySelectorAll('.tree-row[data-path]')) {
        row.dataset.hiddenDirect = String(directlyHiddenPaths.has(row.dataset.path));
        row.dataset.hiddenInherited = String(inheritedHiddenPaths.has(row.dataset.path));
    }
    updateToolbar();
};

const visibilityUpdateToolbar = updateToolbar;
updateToolbar = function updateToolbarWithInheritedVisibility() {
    visibilityUpdateToolbar();
    const count = selected.size;
    if (!count || selectionKind !== 'directory') return;
    const rows = [...document.querySelectorAll('.tree-row.selected')];
    const inherited = rows.some(row => row.dataset.hiddenInherited === 'true');
    if (inherited) {
        $('hide').disabled = true;
        $('hide').textContent = '由上级隐藏';
        $('hide').title = '请先恢复上级目录；子目录不能覆盖上级隐藏状态';
        return;
    }
    $('hide').title = '';
    if (rows.length === count && rows.every(row => row.dataset.hiddenDirect === 'true')) {
        $('hide').textContent = '恢复';
    }
};
