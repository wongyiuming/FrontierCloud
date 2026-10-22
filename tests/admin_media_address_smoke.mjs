import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Run the complete scripts with controlled DOM and same-origin API doubles.
const files = ['static/js/admin.js', 'static/js/admin-upload-integrity.js', 'static/js/admin-visibility-integrity.js'];
const sources = files.map(file => fs.readFileSync(new URL(`../${file}`, import.meta.url), 'utf8'));
const addresses = ['https://objects.example.test/bucket/', 'https://direct.example.test',
    'https://relay.example.test:8443', null, null];
const displays = ['objects.example.test/bucket', 'direct.example.test',
    'relay.example.test:8443', '地址不可用', 'admin.example.test'];
const payload = ['S3', 'Direct', 'Relay', 'Unknown', 'Local'].map((transport, index) => ({
    kind: 'file', name: `${transport}.mp3`, path: `music/album/${transport}.mp3`, size: 8493465,
    media: true, hideable: false, encrypted: index !== 1, transport,
    storage_address: addresses[index], storage_address_local: transport === 'Local',
}));
function matches(node, selector) {
    if (selector === '.tree-row[data-path]') return node.classList.contains('tree-row') && 'path' in node.dataset;
    if (/^\.[a-z-]+$/.test(selector)) return node.classList.contains(selector.slice(1));
    return false;
}
function element(tag = 'div') {
    const node = {tagName: tag.toUpperCase(), children: [], dataset: {}, value: '', className: '',
        disabled: false, textContent: '', parentNode: null, addEventListener() {},
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
        append(...children) { children.forEach(child => this.appendChild(child)); },
        replaceChildren(...children) { this.children = []; this.append(...children); },
        remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); },
        setAttribute(name, value) { this[name] = String(value); },
        querySelectorAll(selector) { return this.children.flatMap(child => [
            ...(matches(child, selector) ? [child] : []), ...child.querySelectorAll(selector)]); },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    };
    node.classList = {contains: name => node.className.split(' ').includes(name),
        add: name => { if (!node.classList.contains(name)) node.className += ` ${name}`; },
        remove: name => { node.className = node.className.split(' ').filter(value => value !== name).join(' '); },
        toggle: (name, force) => { (force ?? !node.classList.contains(name)) ? node.classList.add(name) : node.classList.remove(name); }};
    Object.defineProperty(node, 'options', {get: () => node.children});
    Object.defineProperty(node, 'innerHTML', {set(value) {
        this.children = [];
        // Only the real tree row's three direct markup children are needed.
        if (this.className.startsWith('tree-row') && value) {
            for (const className of ['kind', 'tree-label']) {
                const child = element('span'); child.className = className; this.append(child);
            }
            const size = value.match(/<small>([\s\S]*?)<\/small>/);
            if (size) { const child = element('small'); child.textContent = size[1]; this.append(child); }
        }
    }, get() { return ''; }});
    return node;
}
const flush = async () => { for (let count = 0; count < 12; count++) await new Promise(resolve => setImmediate(resolve)); };
function fixture(holdTree) {
    const elements = new Map(), listeners = new Map(), requests = [], intervals = [];
    const byId = id => { if (id === 'usersPanel') return null;
        if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
    const document = {cookie: '', hidden: false, readyState: 'loading', body: element(), getElementById: byId,
        createElement: element, querySelector() { return null; },
        addEventListener(name, callback, options = {}) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push({callback, once: options.once});
        },
        querySelectorAll(selector) { return byId('tree').querySelectorAll(selector); }};
    let releaseStatus, releaseTree;
    const status = new Promise(resolve => { releaseStatus = resolve; });
    const gate = new Promise(resolve => { releaseTree = resolve; });
    const response = data => ({status: 200, ok: true, json: async () => data});
    const context = vm.createContext({document, location: {origin: 'https://admin.example.test', href: 'https://admin.example.test/admin'},
        URL, URLSearchParams, AbortController, console, setTimeout, clearTimeout,
        setInterval: (_callback, delay) => { intervals.push(delay); return intervals.length; }, clearInterval() {},
        alert: text => { throw new Error(text); },
        fetch: async url => { const value = String(url); requests.push(value);
            if (value.endsWith('/admin/status')) return response(await status);
            if (value.includes('/admin/tree')) { if (holdTree) await gate; return response({path: 'music/album', items: payload}); }
            if (value.endsWith('/storage-pool')) return response({standalone: false, members: []});
            return response({items: []}); },
    });
    context.window = context;
    const run = text => vm.runInContext(text, context);
    const load = index => vm.runInContext(sources[index], context, {filename: files[index]});
    load(0);
    // Seed a media scope before the controlled startup request resumes.
    run("currentPath='music/album'");
    const ready = () => {
        document.readyState = 'interactive';
        for (const listener of [...(listeners.get('DOMContentLoaded') || [])]) {
            if (listener.once) listeners.set('DOMContentLoaded', listeners.get('DOMContentLoaded').filter(value => value !== listener));
            listener.callback();
        }
    };
    return {run, load, byId, requests, intervals, ready,
        releaseStatus: () => releaseStatus({node_role: 'Master', credential_kind: 'persistent',
            csrf_cookie_name: 'fixture-csrf', limits: {max_upload_file_size: 1234}}), releaseTree};
}
function assertRows(f, phase) {
    const rows = f.byId('tree').children.filter(row => row.dataset.path);
    assert.equal(rows.length, payload.length, `${phase}: all files must be rendered`);
    for (const row of rows) {
        const classes = row.children.map(child => child.className);
        const size = row.children.findIndex(child => child.tagName === 'SMALL' && !child.className);
        const address = classes.indexOf('media-node-address'), encrypted = classes.indexOf('media-encryption-badge');
        assert.ok(size >= 0 && size < address && address < encrypted, `${phase}: size/address/encryption order for ${row.dataset.path}`);
        const index = payload.findIndex(item => item.path === row.dataset.path);
        assert.equal(row.children[size].textContent, '8.1 MB');
        assert.equal(row.children[address].textContent, displays[index]);
        assert.equal(row.children[encrypted].textContent, payload[index].encrypted ? '加密落盘' : '明文落盘');
        assert.equal(classes.filter(value => value === 'media-node-address').length, 1);
    }
}
for (const order of ['responses-ready-before-hooks', 'hooks-before-status', 'tree-response-delayed']) {
    const f = fixture(order === 'tree-response-delayed');
    if (order !== 'hooks-before-status') { f.releaseStatus(); await flush(); }
    assert.equal(f.requests.filter(url => /\/admin\/(?:status|tree)/.test(url)).length, 0,
        `${order}: startup must wait for DOMContentLoaded while ordinary hook scripts are still loading`);
    f.load(1); f.load(2);
    f.ready();
    await flush();
    if (order === 'hooks-before-status') f.releaseStatus();
    if (order === 'tree-response-delayed') {
        assert.ok(f.requests.some(url => url.includes('/admin/tree?')));
        assert.equal(f.byId('tree').children.length, 0);
        f.releaseTree();
    }
    await flush();
    assertRows(f, `${order}:initial`);
    assert.equal(f.run('csrfCookieName'), 'fixture-csrf');
    assert.equal(f.run('uploadLimits.max_upload_file_size'), 1234);
    assert.deepEqual(f.intervals, [10000, 15000], 'storage and security polling must remain intact');
    f.ready(); await flush();
    assert.equal(f.requests.filter(url => url.endsWith('/admin/status')).length, 1, 'initialization must run once');
    await f.run('renderTree()'); assertRows(f, `${order}:refresh`);
    f.byId('mediaSearch').value = 'mp3';
    await f.run('renderTree()'); assertRows(f, `${order}:search`);
    assert.ok(f.requests.some(url => url.includes('/tree/search?q=mp3&path=music%2Falbum')));
}
console.log('admin-media-address-smoke-ok: three DOM ready/response orders, first render, refresh, search, address ordering and unchanged initialization');
