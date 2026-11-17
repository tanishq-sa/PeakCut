const assert = require('node:assert/strict');
const test = require('node:test');
const ui = require('../web/studio-tools.js');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('delta updates keep untouched jobs and remove deleted jobs', () => {
  const old = [{id:'a', title:'A'}, {id:'b', title:'B'}];
  const result = ui.mergeRows(old, [{id:'a', title:'New'}], ['b']);
  assert.deepEqual(result, [{id:'a', title:'New'}]);
  assert.equal(old[0].title, 'A');
});
test('unchanged cards keep their identity', () => {
  const kept = {id:'a'};
  assert.equal(ui.mergeRows([kept], [{id:'b'}], [])[0], kept);
});
test('trim handles cannot cross or leave saved media', () => {
  assert.deepEqual(ui.trim(8, 7, 2, 12, 'start'), {start:4, end:7});
  assert.deepEqual(ui.trim(-1, 30, 2, 12, 'end'), {start:2, end:12});
});
test('caption edits preserve times and reject invalid timing', () => {
  assert.deepEqual(ui.captionWords([{w:'Corrected', s:'1', e:'2'}]), [{w:'Corrected', s:1, e:2}]);
  assert.throws(() => ui.captionWords([{w:'bad', s:3, e:1}]));
  assert.throws(() => ui.captionWords([{w:'bad', s:NaN, e:2}]));
});

test('processing log displays errors as text and opens for failed jobs', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const helper = source.slice(source.indexOf('function jobLogsHtml('), source.indexOf('function jobHtml('));
  const context = vm.createContext({esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')});
  vm.runInContext(helper, context);
  const html = context.jobLogsHtml({status:'error', logs:[{time:null, level:'error', message:'<img src=x onerror=alert(1)>'}]});
  assert.match(html, /class="job-logs" open/);
  assert.match(html, /Earlier  ERROR · &lt;img/);
  assert.doesNotMatch(html, /<img/);
});

test('YouTube posting labels distinguish transfer progress from remote processing', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const helper = source.slice(source.indexOf('function postLabel('), source.indexOf('function postPills('));
  const context = vm.createContext({});
  vm.runInContext(helper, context);
  const upload = {platform:'youtube', status:'posting', upload_bytes:40, upload_total:100, upload_stage:'uploading'};
  assert.equal(context.postLabel(upload), 'Uploading to YouTube · 40%');
  assert.match(context.postLabel({...upload, upload_stage:'reconnecting'}), /Reconnecting.*40%/);
  assert.match(context.postLabel({...upload, status:'youtube_processing'}), /Uploaded · YouTube processing/);
  assert.equal(context.postLabel({platform:'youtube', status:'posting'}), 'Connecting to YouTube');
});
