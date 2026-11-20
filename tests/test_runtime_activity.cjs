const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Activity = require('../crates/anvildroid-gui/src-frontend/runtime-activity.js');

const ID = 'r-' + '1'.repeat(32), OTHER = 'r-' + '2'.repeat(32);
const GAME = 'com.example.game';
const proc = (pid, ticks, extra = {}) => ({pid, start:1000 + pid, ticks, uid:10000 + pid, rss_kb:100 * 1024, name:'app.number' + pid, ...extra});
const report = (extra = {}) => ({runtime_id:ID, uptime:1000, ticks:100, cpus:4, unavailable:{},
  processes:[proc(1, 500, {name:GAME}), proc(2, 100, {uid:1000, name:'system_server', rss_kb:300 * 1024})],
  focus:{package:GAME, activity:GAME + '.Main'}, frames:{pid:1, total:600, since:'77', uptime_ms:900000},
  surface:{layer:`SurfaceView[${GAME}/${GAME}.Main](BLAST)#45`, kind:'surfaceview', refresh_hz:60, fps:58.4, newest:'5000', frames:127}, ...extra});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };

function dom() {
  const listeners = {};
  function node(tag) {
    return {tag, children:[], textContent:'', className:'', title:'', hidden:false, attributes:{}, listeners:{}, parentNode:null,
      append(...items) { for (const item of items) { item.remove?.(); item.parentNode = this; this.children.push(item); } },
      replaceChildren(...items) { for (const item of this.children) item.parentNode = null; this.children = []; this.append(...items); },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      set innerHTML(_) { throw new Error('Dynamic HTML is forbidden'); },
      click() { return this.listeners.click?.(); },
      all(match, found = []) { for (const child of this.children) { if (match(child)) found.push(child); child.all(match, found); } return found; },
      text() { return this.hidden ? '' : [this.textContent, ...this.children.map(child => child.text())].join(' '); },
    };
  }
  return {createElement:node, visibilityState:'visible', listeners,
    addEventListener(name, handler) { listeners[name] = handler; },
    show(state) { this.visibilityState = state; listeners.visibilitychange(); }};
}
// Fake timers: nothing runs until the test says so.
function clock() {
  let next = 1;
  const pending = new Map();
  return {pending, set(task, delay) { pending.set(next, {task, delay}); return next++; }, clear(id) { pending.delete(id); },
    delays() { return [...pending.values()].map(item => item.delay); },
    run() { const items = [...pending.values()]; pending.clear(); for (const item of items) item.task(); }};
}
function panel(handler) {
  const sent = [], document = dom(), timers = clock(), host = document.createElement('div');
  const tool = Activity.create({document, timers, request:async request => { sent.push(request); return handler(request, sent.length); }});
  const one = name => host.all(item => item.className.split(' ').includes(name))[0];
  return {tool, host, sent, document, timers, one, rows:() => host.all(item => item.tag === 'tr').slice(1).map(row => row.children.map(cell => cell.text().replace(/\s+/g, ' ').trim()))};
}
const running = (id = ID) => ({id, name:'Phone', state:'Running'});

test('reports are validated before anything is shown', () => {
  assert.equal(Activity.validate(report(), ID).processes.length, 2);
  assert.ok(Activity.validate(report({processes:null, focus:null, frames:null, surface:null, cpus:null, unavailable:{focus:'No app is in front.'}}), ID));
  assert.ok(Activity.validate(report({surface:{layer:'a/b#1', kind:'window', refresh_hz:74, fps:null, newest:null, frames:0}}), ID));
  for (const bad of [null, 'x', {}, report({runtime_id:OTHER}), report({uptime:'1'}), report({uptime:NaN}), report({ticks:0}), report({cpus:0}),
      report({unavailable:null}), report({unavailable:{focus:5}}), report({processes:'none'}), report({processes:[proc(1, -1)]}),
      report({processes:[proc(1, 1, {name:''})]}), report({processes:[proc(1.5, 1)]}), report({processes:[proc(1, 1, {uid:'root'})]}),
      report({processes:[null]}), report({focus:{package:'no dots', activity:'x'}}), report({focus:{package:'a.b;c', activity:'x'}}),
      report({frames:{pid:1, total:-1, since:'', uptime_ms:1}}), report({frames:{pid:1, total:1, since:5, uptime_ms:1}}),
      report({surface:{layer:'x', kind:'screen', refresh_hz:60, fps:1, newest:'1', frames:1}}),
      report({surface:{layer:'x', kind:'window', refresh_hz:60, fps:-1, newest:'1', frames:1}}),
      report({surface:{layer:'x', kind:'window', refresh_hz:60, fps:30, newest:null, frames:1}}),
      report({surface:{layer:'x', kind:'window', refresh_hz:0, fps:null, newest:null, frames:0}})])
    assert.throws(() => Activity.validate(bad, ID), /Invalid activity response/);
});

test('CPU share comes from two answers and uses the device CPU count', () => {
  const first = report(), second = report({uptime:1002, processes:[proc(1, 900, {name:GAME}), proc(2, 100, {uid:1000, name:'system_server'})]});
  // First sample: nothing to compare with.
  assert.deepEqual(Activity.usage(null, first, 4).rows.map(row => row.cpu), [null, null]);
  assert.equal(Activity.usage(null, first, 4).measured, false);
  // 400 ticks at 100 per second over 2 s is 2 CPUs: half of 4.
  const {rows, measured} = Activity.usage(first, second, 4);
  assert.equal(measured, true);
  assert.deepEqual(rows.map(row => row.cpu), [50, 0]);
  assert.equal(Activity.usage(first, second, 2).rows[0].cpu, 100);
  assert.equal(Activity.usage(first, second, 1).rows[0].cpu, 100, 'capped at all CPUs');
  assert.equal(Activity.usage(first, second, null).rows[0].cpu, 200, 'per core when the CPU count is unknown');
});

test('a reused PID, a counter going backwards and odd intervals are never compared', () => {
  const first = report();
  const reused = report({uptime:1002, processes:[proc(1, 30, {start:50}), proc(2, 100)]});
  assert.deepEqual(Activity.usage(first, reused, 4).rows.map(row => row.cpu), [null, 0], 'same PID, older unrelated process');
  // A process that started after the previous answer used all of its time in this interval.
  const fresh = report({uptime:1002, processes:[proc(1, 40, {start:100100}), proc(9, 20, {start:100150})]});
  assert.deepEqual(Activity.usage(first, fresh, 4).rows.map(row => row.cpu), [5, 2.5]);
  const backwards = report({uptime:1002, processes:[proc(1, 499), proc(2, 100)]});
  assert.deepEqual(Activity.usage(first, backwards, 4).rows.map(row => row.cpu), [null, 0]);
  for (const uptime of [1000, 1000.05, 999, 5000])
    assert.equal(Activity.usage(first, report({uptime}), 4).measured, false, 'interval ' + uptime);
  assert.equal(Activity.usage(first, report({uptime:1002, ticks:250}), 4).measured, false, 'tick length changed');
  assert.equal(Activity.usage(report({processes:null}), report({uptime:1002}), 4).measured, false);
  assert.deepEqual(Activity.usage(first, report({uptime:1002, processes:null}), 4).rows, []);
});

test('apps and busy or large processes are listed, the rest is summed', () => {
  const rows = [
    {...proc(1, 0, {name:'idle.app', rss_kb:10 * 1024}), cpu:0},
    {...proc(2, 0, {uid:0, name:'init', rss_kb:6 * 1024}), cpu:0},
    {...proc(3, 0, {uid:1000, name:'system_server', rss_kb:300 * 1024}), cpu:2},
    {...proc(4, 0, {uid:0, name:'surfaceflinger', rss_kb:20 * 1024}), cpu:7.5},
    {...proc(5, 0, {name:'busy.game', rss_kb:800 * 1024}), cpu:40},
    {...proc(6, 0, {uid:1036, name:'logd', rss_kb:6 * 1024}), cpu:0.2},
  ];
  const {shown, rest} = Activity.pick(rows);
  assert.deepEqual(shown.map(row => row.name), ['busy.game', 'surfaceflinger', 'system_server', 'idle.app']);
  assert.equal(rest.count, 2);
  assert.equal(rest.rss_kb, 12 * 1024);
  assert.ok(Math.abs(rest.cpu - 0.2) < 1e-9);
  // Before the second answer the order is by memory and the summary has no CPU figure.
  const early = Activity.pick(rows.map(row => ({...row, cpu:null})));
  assert.deepEqual(early.shown.map(row => row.name), ['busy.game', 'system_server', 'idle.app']);
  assert.equal(early.rest.cpu, null);
  const many = Array.from({length:40}, (_, index) => ({...proc(index + 1, 0), cpu:index}));
  assert.equal(Activity.pick(many).shown.length, 12);
  assert.equal(Activity.pick(many).rest.count, 28);
  assert.equal(Activity.pick(many).shown[0].cpu, 39);
  // Without owner information nothing can be called noise.
  assert.equal(Activity.pick([{...proc(1, 0, {uid:null, rss_kb:10}), cpu:0}]).shown.length, 1);
});

test('names and sizes are presented plainly', () => {
  assert.deepEqual(Activity.splitName('com.google.android.googlequicksearchbox:search'), {main:'com.google.android.googlequicksearchbox', part:'search'});
  assert.deepEqual(Activity.splitName('system_server'), {main:'system_server', part:''});
  assert.deepEqual(Activity.splitName(':odd'), {main:':odd', part:''});
  assert.deepEqual(Activity.splitName('odd:'), {main:'odd:', part:''});
  assert.equal(Activity.formatMemory(160728), '157 MiB');
  assert.equal(Activity.formatMemory(3 * 1024 ** 2), '3.00 GiB');
  assert.equal(Activity.formatMemory(12), '1 MiB');
  assert.equal(Activity.formatMemory(undefined), '—');
  assert.equal(Activity.capacityOf({resources:{effective:{cpu_quota_count:2}, host:{cpu_count:16}}}, report()), 2);
  assert.equal(Activity.capacityOf({resources:{effective:{cpu_quota_count:null}, host:{cpu_count:16}}}, report()), 16);
  assert.equal(Activity.capacityOf({}, report()), 4);
  assert.equal(Activity.capacityOf({}, report({cpus:null})), null);
});

test('frame rate: first answer measures, then the faster source wins', () => {
  const first = report();
  assert.deepEqual(Activity.frameRate(null, first), {fps:null, refresh:60, measuring:true, reason:''});
  // The toolkit drew 20 frames in 2 s, the game surface presented at 58.4.
  const next = report({frames:{pid:1, total:620, since:'77', uptime_ms:902000}, surface:{...first.surface, newest:'9000'}});
  assert.deepEqual(Activity.frameRate(first, next), {fps:58.4, refresh:60, measuring:false, reason:''});
  // Toolkit only (no surface): 120 frames in 2 s.
  const toolkit = extra => report({surface:null, unavailable:{surface:'This app has no surface to measure.'}, ...extra});
  assert.equal(Activity.frameRate(toolkit(), toolkit({frames:{pid:1, total:720, since:'77', uptime_ms:902000}})).fps, 60);
  assert.equal(Activity.frameRate(toolkit(), toolkit({frames:{pid:1, total:720, since:'77', uptime_ms:902000}})).refresh, null);
  // Surface only (a game that draws by itself).
  const game = extra => report({frames:null, unavailable:{frames:'This app does not report its frames.'}, ...extra});
  assert.equal(Activity.frameRate(game(), game({surface:{...first.surface, fps:30, newest:'9000'}})).fps, 30);
});

test('frame rate: a still screen is 0, a reset counter is unknown', () => {
  const first = report();
  const still = Activity.frameRate(first, report({frames:{...first.frames, uptime_ms:902000}}));
  assert.equal(still.fps, 0);
  assert.equal(still.measuring, false);
  assert.match(still.reason, /^No new frames since the last update\./);
  // The surface never presented a frame and there is no toolkit counter: 0 at once.
  const empty = report({frames:null, surface:{...first.surface, fps:null, newest:null, frames:0}});
  assert.equal(Activity.frameRate(null, empty).fps, 0);
  // Counter went backwards, statistics restarted or the process is another one: measure again, never negative.
  for (const frames of [{pid:1, total:10, since:'77', uptime_ms:902000}, {pid:1, total:900, since:'88', uptime_ms:902000},
      {pid:5, total:900, since:'77', uptime_ms:902000}, {pid:1, total:900, since:'77', uptime_ms:900010}, {pid:1, total:900, since:'77', uptime_ms:9e9}]) {
    const rate = Activity.frameRate(first, report({frames}));
    assert.deepEqual([rate.fps, rate.measuring], [null, true], JSON.stringify(frames));
  }
  // ...unless the surface shows new frames in the same answer.
  assert.equal(Activity.frameRate(first, report({frames:{pid:5, total:1, since:'9', uptime_ms:902000}, surface:{...first.surface, newest:'9000'}})).fps, 58.4);
  // Another app came to the front: its first answer measures.
  const other = report({focus:{package:'org.other.app', activity:'org.other.app.Home'}, frames:{pid:1, total:900, since:'77', uptime_ms:902000}});
  assert.equal(Activity.frameRate(first, other).measuring, true);
  assert.equal(Activity.frameRate(first, report({surface:{...first.surface, fps:99999, newest:'9000'}})).fps, null);
});

test('frame rate: unknown comes with a reason', () => {
  const none = report({focus:null, frames:null, surface:null, unavailable:{focus:'No app is in front.', frames:'No app is in front.', surface:'No app is in front.'}});
  assert.deepEqual(Activity.frameRate(report(), none), {fps:null, refresh:null, measuring:false, reason:'No app is in front.'});
  const silent = report({frames:null, surface:null, unavailable:{frames:'This app does not report its frames.', surface:'This app has no surface to measure.'}});
  assert.deepEqual(Activity.frameRate(report(), silent), {fps:null, refresh:null, measuring:false, reason:'This app does not report its frames.'});
  assert.equal(Activity.frameRate(null, report({frames:null, surface:null})).reason, 'This app does not report its frames.');
});

test('panel: memory at once, CPU and frames after the second answer', async () => {
  const init = proc(3, 0, {uid:0, name:'init', rss_kb:2048});
  const answers = [report({processes:[...report().processes, init]}), report({uptime:1002, processes:[proc(1, 900, {name:GAME + ':render'}), proc(2, 100, {uid:1000, name:'system_server', rss_kb:300 * 1024}),
    init], frames:{pid:1, total:720, since:'77', uptime_ms:902000}, surface:{...report().surface, newest:'9000'}})];
  const ui = panel((request, count) => answers[count - 1]);
  ui.tool.mount(ui.host, running());
  assert.match(ui.host.text(), /Reading activity…/);
  await settle();
  assert.deepEqual(ui.sent, [{op:'performance_get', id:ID}]);
  assert.deepEqual(ui.rows(), [['system_server', 'measuring…', '300 MiB'], [GAME + ' In front', 'measuring…', '100 MiB']]);
  assert.equal(ui.one('activity-front-rate').textContent, 'measuring…');
  assert.equal(ui.one('activity-front-name').textContent, GAME);
  assert.equal(ui.one('activity-front-name').title, GAME + '/' + GAME + '.Main');
  assert.equal(ui.one('activity-rest').textContent, '1 more process uses 2 MiB.');
  assert.deepEqual(ui.timers.delays(), [2000]);

  ui.timers.run();
  await settle();
  assert.deepEqual(ui.rows(), [[GAME + ' render In front', '50.0%', '100 MiB'], ['system_server', '0.0%', '300 MiB']]);
  assert.equal(ui.host.all(item => item.tag === 'td')[0].title, GAME + ':render (PID 1)');
  assert.equal(ui.one('activity-front-rate').textContent, '60 FPS · display 60 Hz');
  assert.equal(ui.one('activity-rest').textContent, '1 more process uses 0.0% CPU and 2 MiB.');
  assert.match(ui.one('activity-note').textContent, /CPU 100% means all 4 CPUs of this device are busy\. Measuring does not keep this device awake\./);
  assert.equal(ui.one('activity-notice').hidden, true);
  assert.equal(ui.host.all(item => item.tag === 'button')[0].hidden, true);
});

test('panel: parts that are missing say why and the rest stays', async () => {
  const ui = panel(() => report({processes:null, focus:null, frames:null, surface:null,
    unavailable:{processes:'Android did not list its processes.', focus:'No app is in front.', frames:'No app is in front.', surface:'No app is in front.'}}));
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.one('activity-front-name').textContent, '—');
  assert.equal(ui.one('activity-front-rate').textContent, '—');
  assert.equal(ui.one('activity-front-hint').textContent, 'No app is in front.');
  assert.equal(ui.one('activity-table').hidden, true);
  assert.equal(ui.one('activity-table-hint').textContent, 'Android did not list its processes.');
  assert.deepEqual(ui.timers.delays(), [2000], 'still polling');
});

test('panel: re-rendering the page does not refetch, rebuild or overlap', async () => {
  let release;
  const ui = panel((request, count) => count === 1 ? new Promise(resolve => { release = resolve; }) : report({uptime:1000 + count}));
  ui.tool.mount(ui.host, running());
  ui.tool.mount(ui.host, running());
  const second = ui.document.createElement('div');
  ui.tool.mount(second, running());
  await settle();
  assert.equal(ui.sent.length, 1, 'one request while the first is on its way');
  assert.equal(ui.timers.pending.size, 0);
  assert.equal(second.children.length, 1);
  assert.equal(ui.host.children.length, 0, 'the same panel moved to the new host');
  release(report());
  await settle();
  ui.tool.mount(second, running());
  ui.tool.mount(second, running());
  await settle();
  assert.equal(ui.sent.length, 1);
  assert.deepEqual(ui.timers.delays(), [2000]);
  ui.timers.run();
  await settle();
  assert.equal(ui.sent.length, 2);
});

test('panel: closing the section stops everything and drops a late answer', async () => {
  let release;
  const ui = panel((request, count) => count === 2 ? new Promise(resolve => { release = resolve; }) : report({uptime:1000 + 2 * count}));
  ui.tool.mount(ui.host, running());
  await settle();
  ui.timers.run();
  await settle();
  assert.equal(ui.sent.length, 2);
  ui.tool.deactivate();
  assert.equal(ui.host.children.length, 0);
  release(report({uptime:1002, processes:[proc(77, 1, {name:'late.answer'})]}));
  await settle();
  assert.equal(ui.timers.pending.size, 0, 'no poll after the section closed');
  assert.equal(ui.sent.length, 2);

  // Opening again starts from a first sample.
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.sent.length, 3);
  assert.ok(ui.rows().every(row => row[1] === 'measuring…'));
  assert.doesNotMatch(ui.host.text(), /late\.answer/);
  ui.tool.deactivate();
  ui.timers.run();
  await settle();
  assert.equal(ui.sent.length, 3);
});

test('panel: reopening while a request is on its way waits for it instead of overlapping', async () => {
  const waiting = [];
  const ui = panel(() => new Promise(resolve => waiting.push(resolve)));
  ui.tool.mount(ui.host, running());
  ui.tool.deactivate();
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.sent.length, 1);
  waiting[0](report());
  await settle();
  assert.match(ui.host.text(), /Reading activity…/, 'the disowned answer is not shown');
  assert.deepEqual(ui.timers.delays(), [0]);
  ui.timers.run();
  await settle();
  assert.equal(ui.sent.length, 2);
  waiting[1](report());
  await settle();
  assert.equal(ui.rows().length, 2);
});

test('panel: another device starts over and never mixes samples', async () => {
  const ui = panel(request => report({runtime_id:request.id, uptime:request.id === ID ? 1000 : 1002,
    processes:[proc(1, request.id === ID ? 500 : 900)]}));
  ui.tool.mount(ui.host, running());
  await settle();
  ui.tool.mount(ui.host, running(OTHER));
  assert.match(ui.host.text(), /Reading activity…/);
  await settle();
  assert.deepEqual(ui.sent.map(item => item.id), [ID, OTHER]);
  assert.deepEqual(ui.rows(), [['app.number1', 'measuring…', '100 MiB']]);
  assert.equal(ui.timers.pending.size, 1);
});

test('panel: an answer for another device is refused', async () => {
  const ui = panel(() => report({runtime_id:OTHER}));
  ui.tool.mount(ui.host, running());
  await settle();
  assert.match(ui.one('activity-notice').textContent, /^Could not read the activity of this device\. Invalid activity response or another device\. Use Retry\.$/);
  assert.equal(ui.one('activity-content').hidden, true);
});

test('panel: a hidden window pauses and a visible one starts fresh', async () => {
  const ui = panel((request, count) => report({uptime:1000 + 2 * count}));
  ui.tool.mount(ui.host, running());
  await settle();
  ui.document.show('hidden');
  assert.equal(ui.timers.pending.size, 0);
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.sent.length, 1, 'no poll while hidden');
  ui.document.show('visible');
  await settle();
  assert.equal(ui.sent.length, 2);
  assert.ok(ui.rows().every(row => row[1] === 'measuring…'), 'the long pause is not averaged');
  assert.deepEqual(ui.timers.delays(), [2000]);
  // Visibility changes mean nothing while the section is closed.
  ui.tool.deactivate();
  ui.document.show('hidden');
  ui.document.show('visible');
  await settle();
  assert.equal(ui.sent.length, 2);
});

test('panel: an error stops polling until Retry', async () => {
  let fail = true;
  const ui = panel(() => { if (fail) throw new Error('Android sh timed out'); return report(); });
  ui.tool.mount(ui.host, running());
  await settle();
  const notice = ui.one('activity-notice'), retry = ui.host.all(item => item.tag === 'button')[0];
  assert.equal(notice.textContent, 'Could not read the activity of this device. Android sh timed out. Use Retry.');
  assert.equal(notice.attributes.role, 'alert');
  assert.match(notice.className, /runtime-feedback error/);
  assert.equal(retry.hidden, false);
  assert.equal(ui.timers.pending.size, 0);
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.sent.length, 1, 're-rendering does not retry by itself');
  fail = false;
  retry.click();
  await settle();
  assert.equal(ui.sent.length, 2);
  assert.equal(notice.hidden, true);
  assert.equal(retry.hidden, true);
  assert.equal(ui.rows().length, 2);
  assert.deepEqual(ui.timers.delays(), [2000]);
});

test('panel: a sleeping or stopped device gets a calm hint and no polling', async () => {
  const ui = panel(() => report());
  for (const [state, hint] of [['Frozen', /^This device is asleep\. Open an app or its Android window to see activity\.$/],
      ['Stopped', /^Start this device to see its activity\.$/], ['Prepared', /^Start this device/], ['Stopping', /^Start this device/]]) {
    ui.tool.mount(ui.host, {id:ID, name:'Phone', state});
    await settle();
    const notice = ui.one('activity-notice');
    assert.match(notice.textContent, hint);
    assert.equal(notice.attributes.role, 'status');
    assert.doesNotMatch(notice.className, /error/);
  }
  assert.equal(ui.sent.length, 0);
  assert.equal(ui.timers.pending.size, 0);

  // It runs again: polling starts. It falls asleep: polling stops and the old sample is dropped.
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.sent.length, 1);
  ui.tool.mount(ui.host, {id:ID, name:'Phone', state:'Frozen'});
  assert.equal(ui.timers.pending.size, 0);
  ui.timers.run();
  await settle();
  assert.equal(ui.sent.length, 1);
  ui.tool.mount(ui.host, running());
  await settle();
  assert.equal(ui.sent.length, 2);
  assert.ok(ui.rows().every(row => row[1] === 'measuring…'));
});

test('panel: the controller saying asleep is a hint, not an error', async () => {
  let message = 'Runtime controller: This device is asleep. Open an app or the Android window, then retry.';
  const ui = panel(() => { throw new Error(message); });
  ui.tool.mount(ui.host, running());
  await settle();
  const notice = ui.one('activity-notice');
  assert.equal(notice.textContent, 'This device is asleep. Open an app or its Android window to see activity.');
  assert.doesNotMatch(notice.className, /error/);
  assert.equal(ui.timers.pending.size, 0);
  message = 'Start this device first.';
  ui.host.all(item => item.tag === 'button')[0].click();
  await settle();
  assert.equal(notice.textContent, 'Start this device to see its activity.');
  assert.equal(ui.sent.length, 2);
  // The sentences must stay the controller's.
  const source = fs.readFileSync('services/runtime-inspect.py', 'utf8');
  assert.match(source, /ASLEEP = 'This device is asleep\. /);
  assert.match(source, /STOPPED = 'Start this device first\.'/);
});

test('panel text and styles follow the app rules', () => {
  const source = fs.readFileSync('crates/anvildroid-gui/src-frontend/runtime-activity.js', 'utf8');
  assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML/);
  // Class names, the op field and file names aside, the word "runtime" never reaches the user.
  assert.doesNotMatch(source.replace(/runtime[-_][a-z_.-]+|AnvilRuntimeActivity/g, ''), /runtime/i, 'UI text says device');
  const css = fs.readFileSync('crates/anvildroid-gui/src-frontend/layout.css', 'utf8');
  const block = css.slice(css.indexOf('/* Device settings → Activity'));
  const mine = block.slice(0, block.indexOf('\n/*', 5) < 0 ? undefined : block.indexOf('\n/*', 5));
  assert.match(mine, /\.activity-table \.activity-number \{[^}]*text-align: right;[^}]*tabular-nums/);
  assert.match(mine, /\.activity-name-main \{[^}]*text-overflow: ellipsis/);
  assert.match(mine, /\.runtime-activity \[hidden\] \{ display: none !important; \}/);
  assert.doesNotMatch(mine, /#[0-9a-fA-F]{3,8}\b|rgb\(/);
});
