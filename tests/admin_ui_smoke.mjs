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
            if (String(url).includes('/logs')) return {entries: [], secure_transport: true};
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

const adminHtml = fs.readFileSync('static/media/admin.html', 'utf8');
assert(adminHtml.includes('id="currentProgress"'));
assert(adminHtml.includes('id="totalProgress"'));
assert(!adminHtml.includes('Artplayer'));
assert(!adminHtml.includes('id="move"'));

console.log('admin-ui-smoke-ok');
