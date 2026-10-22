import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const diagnostics = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
function element() {
    return {
        children: [], textContent: '', value: 'Follower', dataset: {}, attributes: new Map(),
        classList: {toggle() {}, contains() { return false; }},
        append(...nodes) { this.children.push(...nodes); },
        replaceChildren(...nodes) { this.children = nodes; },
        querySelector() { return element(); }, before() {}, addEventListener() {},
        setAttribute(name, value) { this.attributes.set(name, String(value)); },
        getAttribute(name) { return this.attributes.get(name) ?? null; },
        set innerHTML(_value) { throw new Error('Certificate diagnostics must render as text'); },
    };
}
function byId(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
}
const facts = {enabled: true, state: 'PENDING', mode: 'automatic', validation: 'HTTP-01'};
const node = {role: 'Standalone', node_id: 'test-node', protocol: 3, app_version: 'test', certificate: facts};
const context = vm.createContext({
    document: {getElementById: byId, querySelector: () => null, createElement: element, head: element()},
    api: async path => path.endsWith('/observability') ? {members: [], relationships: []} : node,
    window: {}, console, setInterval: () => 1, clearInterval() {}, setTimeout,
});
vm.runInContext(fs.readFileSync('static/js/nodes.js', 'utf8'), context, {filename: 'static/js/nodes.js'});
function rows(root) {
    if (root.className === 'node-kv') return [root.children];
    return root.children.flatMap(rows);
}
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
const results = [];
for (const code of diagnostics) {
    facts.last_error = code;
    await byId('nodesRefresh').onclick();
    assert.equal(byId('nodeOperationStatus').textContent, '已刷新');
    const root = byId('nodeRelationships');
    const toggle = descendants(root).find(item => item.className === 'node-details-toggle');
    assert(toggle, 'Local certificate must remain inside its node card');
    if (toggle.getAttribute('aria-expanded') !== 'true') {
        assert.equal(rows(root).length, 0, 'Collapsed node must not eagerly render certificate details');
        toggle.onclick();
    }
    assert.equal(toggle.getAttribute('aria-expanded'), 'true');
    const grid = rows(root).find(items => items.some(item => item.textContent === '最近错误'));
    assert(grid, 'Actual certificate panel must be rendered');
    const position = grid.findIndex(item => item.textContent === '最近错误');
    const validation = grid.findIndex(item => item.textContent === '验证入口');
    assert.equal(grid[validation + 1].textContent, 'HTTP-01');
    results.push({code, message: grid[position + 1].textContent});
}
console.log(JSON.stringify(results));
