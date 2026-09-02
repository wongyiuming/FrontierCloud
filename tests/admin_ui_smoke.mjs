import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';


class ClassList {
    constructor() { this.values = new Set(); }
    add(value) { this.values.add(value); }
    remove(value) { this.values.delete(value); }
    toggle(value, force) {
        if (force === undefined ? !this.values.has(value) : force) this.values.add(value);
        else this.values.delete(value);
    }
    contains(value) { return this.values.has(value); }
}


function makeElement() {
    const element = {
        children: [],
        classList: new ClassList(),
        dataset: {},
        disabled: false,
        value: 0,
        textContent: '',
        scrollHeight: 0,
        scrollTop: 0,
        clientHeight: 100,
        appendChild(child) {
            this.children.push(child);
            this.scrollHeight = this.children.length * 20;
        },
        append(...children) { children.forEach(child => this.appendChild(child)); },
        setAttribute(name, value) { this[name] = String(value); },
    };
    Object.defineProperty(element, 'innerHTML', {
        get() { return ''; },
        set(value) { if (value === '') this.children = []; },
    });
    return element;
}


const elements = new Map();
const element = id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
};

class TestFormData {
    constructor() { this.values = []; }
    append(name, value, filename = undefined) { this.values.push({name, value, filename}); }
}

const context = {
    document: {
        cookie: 'admin_csrf=test-csrf',
        hidden: false,
        getElementById: element,
        createElement: makeElement,
        querySelectorAll: () => [],
    },
    location: {href: '', protocol: 'http:'},
    alert: message => { throw new Error(`Unexpected alert: ${message}`); },
    fetch: async url => ({
        status: 200,
        ok: true,
        json: async () => {
            if (String(url).includes('/status')) {
                return {
                    status: 'ok',
                    csrf_cookie_name: 'admin_csrf',
                    limits: {max_upload_file_size: 1000, max_upload_task_files: 10},
                };
            }
            if (String(url).includes('/tree')) return {path: '', items: []};
            return {};
        },
        blob: async () => new Blob(),
    }),
    FormData: TestFormData,
    URL,
    Blob,
    XMLHttpRequest: class {},
    setInterval: () => 1,
    clearInterval: () => {},
    console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/admin.js', 'utf8'), context);
await new Promise(resolve => setTimeout(resolve, 0));

const formatted = vm.runInContext(
    `formatErrorDetail([{loc: ['body', 'files'], msg: 'Field required'}])`,
    context,
);
assert.equal(formatted, 'body.files: Field required');
assert(!formatted.includes('[object Object]'));

vm.runInContext(`
    uploadOne = async (formData, onProgress) => {
        onProgress(0.25);
        onProgress(0.75);
        onProgress(1);
        const pathField = formData.values.find(item => item.name === 'relative_path');
        const fileField = formData.values.find(item => item.name === 'file');
        return {path: pathField ? pathField.value : fileField.value.name};
    };
`, context);

await vm.runInContext(`
    runUploadTask(
        [{name: 'one.wav', size: 400}, {name: 'two.wav', size: 600}],
        ['folder/one.wav', 'folder/two.wav'],
    )
`, context);

assert.equal(element('currentProgress').value, 100);
assert.equal(element('totalProgress').value, 100);
assert.equal(element('currentPercent').textContent, '100%');
assert.equal(element('totalPercent').textContent, '100%');
assert.equal(element('uploadResults').children.length, 2);
assert.match(element('uploadSummary').textContent, /成功 2，失败 0/);

vm.runInContext(`
    selected.clear();
    selectionKind = null;
    let selectionRenderCalls = 0;
    renderTree = async () => { selectionRenderCalls += 1; };
    toggleSelection({name: 'one.wav', path: 'folder/one.wav', kind: 'file'});
    globalThis.selectionRenderCalls = selectionRenderCalls;
    globalThis.selectedAfterClick = selected.has('folder/one.wav');
`, context);
assert.equal(context.selectionRenderCalls, 0);
assert.equal(context.selectedAfterClick, true);

vm.runInContext(`
    renderSecurityList({
        legal_api_count: 21,
        active_ban_count: 1,
        threshold: 5,
        ban_seconds: 86400,
        events: [{
            ip: '203.0.113.9', trigger_count: 6, status: 'active', active: true,
            whitelisted: false, banned_at: '2026-08-15T00:00:00',
            expires_at: '2026-08-16T00:00:00', last_method: 'GET', last_path: '/etc/passwd',
        }],
        whitelist: [{ip: '198.51.100.4', created_at: '2026-08-15T00:00:00', note: 'office'}],
    });
`, context);
assert.equal(element('legalApiCount').textContent, '21');
assert.equal(element('activeBanCount').textContent, '1');
assert.equal(element('banList').children.length, 1);
assert.equal(element('whitelistList').children.length, 1);

const adminHtml = fs.readFileSync('static/media/admin.html', 'utf8');
assert(adminHtml.includes('id="currentProgress"'));
assert(adminHtml.includes('id="totalProgress"'));
assert(!adminHtml.includes('Artplayer'));
assert(!adminHtml.includes('id="move"'));
assert(adminHtml.includes('id="securityPanel"'));
assert(adminHtml.includes('id="banList"'));
assert(adminHtml.includes('/static/js/admin.js?v=20260902-1'));
assert(adminHtml.includes('id="randomKey"'));
assert(adminHtml.includes('id="permanentBanForm"'));
assert(adminHtml.match(/class="module-heading"/g).length === 6);
assert(adminHtml.match(/施工中/g).length === 3);
assert(!adminHtml.includes('logOutput'));
assert(!adminHtml.includes('securityToggle'));

const adminJs = fs.readFileSync('static/js/admin.js', 'utf8');
assert(!adminJs.includes('response.blob()'));
assert(adminJs.includes('anchor.href = url'));

console.log('admin-ui-smoke-ok');
