// A controlled DOM fixture proves state changes, without claiming browser visual acceptance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tagName = 'DIV') {
    this.tagName = tagName;
    this.listeners = new Map();
    this.dataset = {};
    this.value = '';
    this.checked = false;
    this.hidden = false;
    this.open = false;
    this.textContent = '';
    this.parentElement = null;
    this.file = null;
  }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  async emit(type) { return this.listeners.get(type)?.({ target: this }); }
  closest(selector) { return selector === '.file-item' ? this.file : null; }
  scrollIntoView() { this.scrolled = true; }
}

async function main() {
  const ids = Object.fromEntries(['path-filter', 'bucket-filter', 'issue-filter', 'wrap-code',
    'expand-all', 'collapse-all', 'filter-result', 'empty-filter', 'interaction-status'].map(id => [id, new Element()]));
  const files = [new Element(), new Element()];
  files[0].dataset = { path: 'src/one.ts', bucket: '业务', hasIssue: 'yes' };
  files[1].dataset = { path: 'tests/two.test.ts', bucket: '测试', hasIssue: 'no' };
  const details = [new Element('DETAILS'), new Element('DETAILS')];
  const hunk = new Element('DETAILS');
  hunk.file = files[0];
  hunk.parentElement = details[0];
  ids['hunk-F001-H01'] = hunk;
  details.push(hunk);
  const classes = new Set();
  const document = {
    body: { classList: { toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name) } },
    getElementById: id => ids[id],
    querySelectorAll: selector => selector === '.file-item' ? files : selector === 'details[data-review-expand]' ? details : [],
    addEventListener(type, handler) { this[type] = handler; }
  };
  const window = { location: { hash: '' }, addEventListener(type, handler) { this[type] = handler; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets/report.js'), 'utf8'), { document, window });
  await ids['path-filter'].emit('input');
  assert.equal(ids['filter-result'].textContent, '显示 2 / 2 个文件');
  ids['bucket-filter'].value = '测试';
  await ids['bucket-filter'].emit('change');
  assert.equal(files[0].hidden, true);
  assert.equal(files[1].hidden, false);
  ids['bucket-filter'].value = '';
  ids['issue-filter'].checked = true;
  await ids['issue-filter'].emit('change');
  assert.equal(files[0].hidden, false);
  assert.equal(files[1].hidden, true);
  ids['path-filter'].value = 'missing';
  await ids['path-filter'].emit('input');
  assert.equal(ids['empty-filter'].hidden, false);
  await ids['expand-all'].emit('click');
  assert.equal(details.every(detail => detail.open), true);
  await ids['collapse-all'].emit('click');
  assert.equal(details.every(detail => !detail.open), true);
  ids['wrap-code'].checked = true;
  await ids['wrap-code'].emit('change');
  assert.equal(classes.has('wrap-code'), true);
  window.location.hash = '#hunk-F001-H01';
  window.hashchange();
  assert.equal(files[0].hidden, false);
  assert.equal(hunk.open, true);
  assert.equal(details[0].open, true);
  assert.equal(hunk.scrolled, true);
  assert.equal(ids['path-filter'].value, '');
  assert.equal(ids['issue-filter'].checked, false);
  console.log('Controlled DOM: filtering, empty state, expand/collapse, wrap and evidence navigation passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
