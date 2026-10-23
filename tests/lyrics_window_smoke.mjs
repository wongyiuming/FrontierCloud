import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source = fs.readFileSync(new URL('../static/js/lyrics-window.js', import.meta.url), 'utf8');
function element() {
  const classes = new Set();
  let scroll = 0;
  const node = {children: [], dataset: {}, clientHeight: 200, clientWidth: 900, clientTop: 0,
    scrollHeight: 500, scrollWrites: 0, rect: {top: 100, bottom: 300},
    classList: {contains: name => classes.has(name), add: name => classes.add(name),
      remove: name => classes.delete(name), [Symbol.iterator]: () => classes.values()},
    append(...children) { for (const child of children) {child.parentElement = this; this.children.push(child);} },
    appendChild(child) {this.append(child);},
    replaceChildren(...children) {this.children = []; this.append(...children);},
    getBoundingClientRect() {return this.rect;},
    closest(selector) {for (let row = this; row; row = row.parentElement) if (row.classList.contains(selector.slice(1))) return row; return null;},
    querySelector(selector) {
      const matches = row => selector === '.fullscreen-lyric-line.current'
        ? row.classList.contains('fullscreen-lyric-line') && row.classList.contains('current')
        : row.dataset.lyricIndex === selector.match(/^\[data-lyric-index="(\d+)"\]$/)?.[1];
      for (const child of this.children) {if (matches(child)) return child; const found = child.querySelector(selector); if (found) return found;}
      return null;
    },
  };
  Object.defineProperty(node, 'className', {get: () => [...classes].join(' '), set: value => {
    classes.clear(); for (const name of value.split(/\s+/).filter(Boolean)) classes.add(name);
  }});
  Object.defineProperty(node, 'scrollTop', {get: () => scroll, set: value => {scroll = value; node.scrollWrites++;}});
  return node;
}
function fixture() {
  const window = {scrollTo() {assert.fail('lyrics must never scroll the page');}};
  const context = {window, document: {createElement: element}, requestAnimationFrame() {}};
  vm.runInNewContext(source, context);
  const ui = window.FrontierLyricsUI, overlay = element();
  overlay.clientHeight = 800;
  const entries = Array.from({length: 9}, (_, index) => ({text: `Line ${index}`}));
  ui.renderFullscreenLyrics(overlay, entries, -1, overlay);
  const columns = overlay.children;
  const row = index => overlay.querySelector(`[data-lyric-index="${index}"]`);
  return {ui, overlay, columns, row};
}

test('wrapped long lines retain the existing viewport-height font tier', () => {
  const {ui, overlay} = fixture();
  const font = () => [...overlay.classList].find(name => /^fs-\d+$/.test(name));
  const short = Array.from({length: 3}, () => ({text: '歌词'}));
  const long = short.map(() => ({text: '长'.repeat(100)}));
  ui.sizeFullscreenLyrics(overlay, short);
  assert.equal(font(), 'fs-48');
  ui.sizeFullscreenLyrics(overlay, long);
  assert.equal(font(), 'fs-48', 'line length must wrap rather than shrink text');
  overlay.clientHeight = 100;
  ui.sizeFullscreenLyrics(overlay, long);
  assert.equal(font(), 'fs-16', 'preserve the existing height-derived tier');
  overlay.clientWidth = 90;
  ui.sizeFullscreenLyrics(overlay, short);
  assert.equal(font(), 'fs-16', 'column width must not reintroduce no-wrap font scaling');
});

test('a current line below the viewport scrolls only its own column', () => {
  const {ui, overlay, columns, row} = fixture();
  ui.syncFullscreenLyrics(overlay, 0);
  row(4).rect = {top: 280, bottom: 340};
  ui.syncFullscreenLyrics(overlay, 4);
  assert.equal(columns[1].scrollTop, 40);
  assert.equal(columns[0].scrollWrites, 0);
  assert.equal(columns[2].scrollWrites, 0);
  assert.equal(overlay.scrollWrites, 0);
  assert.equal(row(0).classList.contains('current'), false);
  assert.equal(row(4).classList.contains('current'), true);
});

test('a current line above the viewport scrolls only its own column', () => {
  const {ui, overlay, columns, row} = fixture();
  columns[2].scrollTop = 100;
  row(7).rect = {top: 70, bottom: 95};
  ui.syncFullscreenLyrics(overlay, 7);
  assert.equal(columns[2].scrollTop, 70);
  assert.equal(columns[0].scrollWrites, 0);
  assert.equal(columns[1].scrollWrites, 0);
  assert.equal(overlay.scrollWrites, 0);
});

test('an already visible current line does not scroll', () => {
  const {ui, overlay, columns, row} = fixture();
  row(4).rect = {top: 120, bottom: 180};
  ui.syncFullscreenLyrics(overlay, 4);
  assert.ok(columns.every(column => column.scrollWrites === 0));
});

test('columns without vertical overflow do not scroll', () => {
  const {ui, overlay, columns, row} = fixture();
  columns[1].scrollHeight = columns[1].clientHeight;
  row(4).rect = {top: 280, bottom: 340};
  ui.syncFullscreenLyrics(overlay, 4);
  assert.ok(columns.every(column => column.scrollWrites === 0));
});
