import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source = fs.readFileSync(new URL('../static/js/lyrics-window.js', import.meta.url), 'utf8');
function element() {
  const classes = new Set();
  let scroll = 0;
  let text = '';
  const node = {children: [], dataset: {}, clientHeight: 200, clientWidth: 900, clientTop: 0,
    scrollHeight: 500, scrollWrites: 0, textWrites: 0, replacementWrites: 0, rect: {top: 100, bottom: 300},
    classList: {contains: name => classes.has(name), add: name => classes.add(name),
      remove: name => classes.delete(name), [Symbol.iterator]: () => classes.values()},
    append(...children) { for (const child of children) this.insertBefore(child, null); },
    appendChild(child) {this.append(child);},
    insertBefore(child, reference) {
      if (child === reference) return child;
      if (child.parentElement) child.parentElement.removeChild(child);
      const index = reference === null ? this.children.length : this.children.indexOf(reference);
      assert.ok(index >= 0, 'insertBefore reference must belong to its parent');
      this.children.splice(index, 0, child);
      child.parentElement = this;
      return child;
    },
    removeChild(child) {
      const index = this.children.indexOf(child);
      assert.ok(index >= 0, 'removeChild requires an owned child');
      this.children.splice(index, 1);
      child.parentElement = null;
      return child;
    },
    remove() {this.parentElement?.removeChild(this);},
    replaceChildren(...children) {
      this.replacementWrites++;
      for (const child of [...this.children]) this.removeChild(child);
      this.append(...children);
    },
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
  Object.defineProperty(node, 'textContent', {get: () => text, set: value => {text = value; node.textWrites++;}});
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

function inlineFixture() {
  const panel = element(), viewport = element(), track = element(), styles = new Map(), timers = [];
  const cancelledTimers = new Set();
  panel.clientWidth = 300;
  panel.style = {setProperty(name, value) {styles.set(name, value);}};
  track.style = {};
  panel.append(viewport); viewport.append(track);
  function rowElement() {
    const row = element();
    Object.defineProperty(row, 'offsetHeight', {get: () => row.textContent?.length > 20 ? 120 : 32});
    row.getBoundingClientRect = () => {
      const preceding = track.children.slice(0, track.children.indexOf(row)).reduce((sum, child) => sum + child.offsetHeight, 0);
      const translation = Number(track.style.transform?.match(/translateY\((-?[\d.]+)px\)/)?.[1] || 0);
      const top = viewport.rect.top + preceding - viewport.scrollTop + translation;
      return {top, bottom: top + row.offsetHeight};
    };
    return row;
  }
  Object.defineProperty(viewport, 'scrollHeight', {get: () => track.children.reduce((sum, row) => sum + row.offsetHeight, 0)});
  const window = {renderLyricWindow() {}, updateSynchronizedLyrics() {},
    getComputedStyle() {return {lineHeight: '28px', paddingTop: '2px', paddingBottom: '2px'};},
    scrollTo() {assert.fail('lyrics must never scroll the page');}};
  const context = {window, activeLyricEntries: Array.from({length: 12}, () => ({text: '歌词'})), activeLyricIndex: null,
    document: {createElement: rowElement, getElementById: id => ({inlineLyrics: panel, inlineLyricsTrack: track})[id]},
    clearLyricSlide() {
      if (context.lyricSlideTimer !== null) cancelledTimers.add(context.lyricSlideTimer);
      context.lyricSlideTimer = null;
    },
    setActiveLyricTime() {},
    setTimeout(callback) {
      const id = timers.length + 1;
      timers.push(() => {if (!cancelledTimers.has(id)) callback();});
      return id;
    },
    lyricSlideTimer: null, LYRIC_SLIDE_MS: 480, requestAnimationFrame() {}};
  vm.runInNewContext(source, context);
  const current = () => track.children.find(row => row.classList.contains('current'));
  const visible = () => {const row = current().getBoundingClientRect(); return row.top >= viewport.rect.top && row.bottom <= viewport.rect.top + viewport.clientHeight;};
  return {window, context, panel, viewport, track, styles, timers, current, visible};
}

test('inline long and short lines keep the same height-derived font and bounds', () => {
  const {window, context, panel, styles} = inlineFixture();
  window.renderLyricWindow(3);
  const short = styles.get('--sync-lyric-font-size');
  assert.equal(short, `${200 / 7.3}px`);
  context.activeLyricEntries = context.activeLyricEntries.map(() => ({text: '长'.repeat(100)}));
  window.renderLyricWindow(3);
  assert.equal(styles.get('--sync-lyric-font-size'), short, 'long text must wrap instead of shrinking');
  panel.clientWidth = 90;
  window.sizeSynchronizedLyrics();
  assert.equal(styles.get('--sync-lyric-font-size'), short);
  panel.clientHeight = 800;
  window.sizeSynchronizedLyrics();
  assert.equal(styles.get('--sync-lyric-font-size'), '44px');
  assert.equal(styles.get('--sync-lyric-row-height'), `${800 / 7}px`);
  panel.clientHeight = 70;
  window.sizeSynchronizedLyrics();
  assert.equal(styles.get('--sync-lyric-font-size'), '14px');
});

test('wrapped preceding lines reveal the inline current line only within its own viewport', () => {
  const {window, context, panel, viewport, track, visible} = inlineFixture();
  context.activeLyricEntries = context.activeLyricEntries.map((_, index) => ({text: index < 3 ? '长'.repeat(100) : '歌词'}));
  window.renderLyricWindow(3);
  assert.equal(visible(), true);
  assert.ok(viewport.scrollTop > 0);
  assert.equal(panel.scrollWrites, 0);
  assert.equal(track.scrollWrites, 0);
});

test('wrapped unequal-height adjacent lines render directly and keep the new current line visible', () => {
  const {window, context, track, timers, current, visible} = inlineFixture();
  context.activeLyricEntries[3].text = '长'.repeat(100);
  window.renderLyricWindow(3);
  window.animateLyricForward(4);
  assert.equal(current().textContent, '歌词');
  assert.equal(track.children.length, 7);
  assert.equal(track.style.transform, 'translateY(0)');
  assert.equal(timers.length, 0, 'wrapped rows must not combine scrolling with a pending track animation');
  assert.equal(visible(), true);
});

test('ordinary single-line windows retain their measured first-row slide animation', () => {
  const {window, track, timers} = inlineFixture();
  window.renderLyricWindow(3);
  const firstHeight = track.children[0].offsetHeight;
  window.animateLyricForward(4);
  assert.equal(track.children.length, 8);
  assert.equal(track.style.transform, `translateY(-${firstHeight}px)`);
  assert.equal(timers.length, 1);
  timers[0]();
  assert.equal(track.children.length, 7);
  assert.equal(track.style.transform, 'translateY(0)');
});

test('adjacent settled windows retain every overlapping line and its unchanged text', () => {
  const {window, context, track, timers, current} = inlineFixture();
  context.activeLyricEntries = context.activeLyricEntries.map((_, index) => ({text: `Line ${index}`}));
  window.renderLyricWindow(3);
  const original = [...track.children], writes = original.map(row => row.textWrites);
  const replacements = track.replacementWrites;
  window.animateLyricForward(4);
  assert.ok(current() === original[4], 'the next existing line becomes current');
  timers[0]();
  assert.equal(track.children.length, 7);
  for (let index = 0; index < 6; index++) {
    assert.ok(track.children[index] === original[index + 1], 'settling must keep overlapping DOM nodes');
    assert.equal(track.children[index].textWrites, writes[index + 1], 'settling must not replace unchanged text');
  }
  assert.ok(current() === original[4], 'settling must preserve the highlighted node');
  assert.equal(track.replacementWrites, replacements, 'settling must never replace the entire window');
});

test('wrapped mixed-height advances retain overlapping rows without a whole-window replacement', () => {
  const {window, context, track, timers, current, visible} = inlineFixture();
  context.activeLyricEntries = context.activeLyricEntries.map((_, index) => ({text: index === 3 ? '长'.repeat(100) : `Line ${index}`}));
  window.renderLyricWindow(3);
  const original = [...track.children], replacements = track.replacementWrites;
  window.animateLyricForward(4);
  assert.equal(timers.length, 0);
  assert.equal(track.children.length, 7);
  assert.ok(current() === original[4], 'wrapped advances keep the existing current row');
  assert.deepEqual(track.children.slice(0, 6), original.slice(1));
  assert.equal(track.replacementWrites, replacements);
  assert.equal(visible(), true);
});

test('an interrupted slide and a nearby seek reuse rows and cannot run a stale settlement', () => {
  const {window, context, track, timers, current, visible} = inlineFixture();
  context.activeLyricEntries = context.activeLyricEntries.map((_, index) => ({text: `Line ${index}`}));
  window.renderLyricWindow(3);
  const originalCurrent = track.children[5], replacements = track.replacementWrites;
  window.animateLyricForward(4);
  // The coordinator settles the current window before advancing a fast cue.
  window.renderLyricWindow(4);
  window.animateLyricForward(5);
  assert.ok(current() === originalCurrent, 'a fast cue keeps the next existing row');
  window.renderLyricWindow(7);
  const soughtRows = [...track.children];
  assert.equal(context.activeLyricIndex, 7);
  assert.equal(current().textContent, 'Line 7');
  timers.forEach(callback => callback());
  assert.equal(context.activeLyricIndex, 7, 'cancelled timers must not restore an old cue');
  assert.deepEqual(track.children, soughtRows);
  assert.equal(track.replacementWrites, replacements);
  assert.equal(visible(), true);
});

test('a new track and changed cue text cannot retain stale lyric content', () => {
  const {window, context, track, current} = inlineFixture();
  window.renderLyricWindow(3);
  context.activeLyricEntries = Array.from({length: 12}, (_, index) => ({text: `New track ${index}`}));
  window.renderLyricWindow(3);
  assert.equal(current().textContent, 'New track 3');
  assert.deepEqual(track.children.map(row => row.textContent), Array.from({length: 7}, (_, index) => `New track ${index}`));
  context.activeLyricEntries[3].text = 'Updated lyric';
  window.renderLyricWindow(3);
  assert.equal(current().textContent, 'Updated lyric');
});
