const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Logs = require('../crates/anvildroid-gui/src-frontend/runtime-logs.js');

const ID = 'r-' + '1'.repeat(32), OTHER = 'r-' + '2'.repeat(32);
const record = (state = 'Running', id = ID) => ({id, name:'Phone', state});
const line = (index, extra = {}) => ({time:1795077089892 + index, pid:7099, tid:7138, level:'I', tag:'Tag', message:'message ' + index, ...extra});
const report = (lines = [], extra = {}) => ({runtime_id:ID, lines, cursor:lines.length ? `${lines.at(-1).time}000000:0123abcd` : null,
  truncated:false, unparsed:0, note:null, ...extra});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };

function dom() {
  function node(tag) {
    return {tag, children:[], textContent:'', className:'', dataset:{}, attributes:{}, listeners:{}, disabled:false, hidden:false, value:'', parentNode:null,
      scrollTop:0, scrollHeight:0, clientHeight:0,
      append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } },
      replaceChildren(...items) { for (const item of this.children) item.parentNode = null; this.children = []; this.append(...items); },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      fire(name) { return this.listeners[name]?.(); },
      click() { return this.listeners.click?.(); },
      all(match, found = []) { for (const child of this.children) { if (match(child)) found.push(child); child.all(match, found); } return found; },
    };
  }
  return {createElement:node};
}
// Fake timers: `later` only records; the test decides when a poll is due.
function panel(handler, extra = {}) {
  const sent = [], toasts = [], timers = new Map(), saved = [], copied = [];
  let nextTimer = 1;
  const document = dom(), host = document.createElement('div');
  const tool = Logs.create({document,
    request:async request => { sent.push(request); return handler(request, sent); },
    invoke:async (command, args) => { saved.push([command, args]); return {path:args.path, bytes:args.content.length}; },
    toast:(message, kind) => toasts.push([kind, message]),
    dialog:{save:async options => { saved.push(['dialog', options]); return '/home/user/logs.txt'; }},
    clipboard:{writeText:async text => { copied.push(text); }},
    later:(callback, ms) => { const handle = nextTimer++; timers.set(handle, {callback, ms}); return handle; },
    cancel:handle => timers.delete(handle), ...extra});
  const find = match => host.all(match);
  const button = label => find(item => item.tag === 'button' && (label === 'Follow' ? ['Follow', 'Pause'].includes(item.textContent) : item.textContent === label))[0];
  const view = () => find(item => item.className === 'logs-view')[0];
  return {tool, host, sent, toasts, timers, saved, copied, button, view,
    rows:() => view().children, shown:() => view().children.filter(item => !item.hidden).map(item => item.textContent),
    status:() => find(item => item.className === 'hint logs-status')[0].textContent,
    problem:() => find(item => item.className.startsWith('runtime-feedback'))[0],
    select:() => find(item => item.tag === 'select')[0], inputs:() => find(item => item.tag === 'input'),
    async tick() { const [[handle, timer]] = [...timers]; timers.delete(handle); timer.callback(); await settle(); }};
}

test('answers are validated before any line is shown', () => {
  assert.equal(Logs.validate(report([line(1)]), ID).lines.length, 1);
  for (const bad of [null, {}, report([], {runtime_id:OTHER}), report([], {lines:'none'}), report([], {truncated:1}), report([], {cursor:5}),
      report([], {note:{}}), report([line(1, {time:'now'})]), report([line(1, {pid:-1})]), report([line(1, {level:'S'})]),
      report([line(1, {tag:null})]), report([line(1, {message:5})]), report([], {lines:[null]}), report(Array.from({length:2001}, (_, index) => line(index)))])
    assert.throws(() => Logs.validate(bad, ID), /Invalid log answer/);
});

test('a line reads like logcat threadtime in local time and file names are plain', () => {
  const when = new Date(2026, 10, 19, 15, 31, 29, 892).getTime();
  assert.equal(Logs.formatLine({time:when, pid:7099, tid:7138, level:'D', tag:'BootAnimation', message:'Playing'}),
    '11-19 15:31:29.892  7099  7138 D BootAnimation: Playing');
  assert.equal(Logs.formatLine({time:when, pid:98, tid:1408, level:'F', tag:'DEBUG', message:''}), '11-19 15:31:29.892    98  1408 F DEBUG: ');
  assert.equal(Logs.fileName('My Phone/../x', new Date(2026, 0, 2, 3, 4, 5)), 'android-logs-My-Phone-x-20260102-030405.txt');
  assert.equal(Logs.fileName('../..', new Date(2026, 0, 2, 3, 4, 5)), 'android-logs-device-20260102-030405.txt');
});

test('opening loads once, then follows with the cursor and never rebuilds on a re-render', async () => {
  const answers = [report([line(1), line(2)]), report([line(3)]), report([])];
  const ui = panel(() => answers.shift());
  ui.tool.mount(ui.host, record());
  ui.tool.mount(ui.host, record());           // the page re-rendered while the first request is out
  await settle();
  assert.deepEqual(ui.sent, [{op:'logs_get', id:ID, lines:Logs.REQUEST_LINES, level:'I', package:null, since:null}]);
  assert.equal(ui.rows().length, 2);
  assert.match(ui.rows()[0].textContent, /7099 {2}7138 I Tag: message 1$/);
  assert.equal(ui.rows()[0].dataset.level, 'I');
  assert.equal(ui.status(), '2 lines.');
  assert.deepEqual([...ui.timers.values()].map(timer => timer.ms), [Logs.POLL_MS]);
  const first = ui.rows()[0];
  ui.tool.mount(ui.host, record());           // same device, same state: nothing new is asked or scheduled
  await settle();
  assert.equal(ui.sent.length, 1);
  assert.equal(ui.timers.size, 1);
  assert.equal(ui.rows()[0], first);
  await ui.tick();
  assert.equal(ui.sent[1].since, `${line(2).time}000000:0123abcd`);
  assert.equal(ui.rows().length, 3);
  await ui.tick();                            // nothing new keeps the last cursor
  assert.equal(ui.sent[2].since, `${line(3).time}000000:0123abcd`);
  assert.equal(ui.rows().length, 3);
  assert.equal(ui.timers.size, 1);
});

test('two requests never overlap, also across closing and reopening', async () => {
  const waiting = [];
  const ui = panel(() => new Promise(resolve => waiting.push(resolve)));
  ui.tool.mount(ui.host, record());
  await settle();
  assert.equal(ui.sent.length, 1);
  assert.equal(ui.status(), 'Loading logs…');
  ui.button('Follow').click(); ui.button('Follow').click();   // pause and follow again while waiting
  ui.tool.deactivate();
  ui.tool.mount(ui.host, record());
  await settle();
  assert.equal(ui.sent.length, 1);
  waiting.shift()(report([line(1)]));           // the answer for the closed view is dropped
  await settle();
  assert.equal(ui.rows().length, 0);
  assert.equal(ui.sent.length, 2);              // only now the reopened view asks
  assert.equal(ui.sent[1].since, null);
  waiting.shift()(report([line(2)]));
  await settle();
  assert.deepEqual(ui.shown().map(text => text.slice(-9)), ['message 2']);
  assert.equal(ui.timers.size, 1);
});

test('closing the section or changing the device stops every timer and forgets the lines', async () => {
  const ui = panel(request => ({...report([line(1)]), runtime_id:request.id}));
  ui.tool.mount(ui.host, record());
  await settle();
  assert.equal(ui.timers.size, 1);
  ui.tool.deactivate();
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.host.children.length, 0);
  ui.tool.mount(ui.host, record());
  await settle();
  assert.equal(ui.sent.at(-1).since, null);
  assert.equal(ui.rows().length, 1);
  ui.tool.mount(ui.host, record('Running', OTHER));
  assert.equal(ui.rows().length, 0);
  await settle();
  assert.deepEqual([ui.sent.at(-1).id, ui.sent.at(-1).since], [OTHER, null]);
  assert.equal(ui.timers.size, 1);
  assert.equal(ui.rows().length, 1);
});

test('a sleeping or stopped device shows a calm hint and is not polled', async () => {
  const ui = panel(() => report([line(1)]));
  ui.tool.mount(ui.host, record('Frozen'));
  await settle();
  assert.deepEqual([ui.sent.length, ui.timers.size], [0, 0]);
  assert.match(ui.status(), /^This device is asleep\. Open an app or the Android window/);
  assert.equal(ui.problem().hidden, true);
  ui.tool.mount(ui.host, record('Stopped'));
  await settle();
  assert.deepEqual([ui.sent.length, ui.timers.size], [0, 0]);
  assert.equal(ui.status(), 'Start this device to see its logs.');
  ui.tool.mount(ui.host, record('Running'));
  await settle();
  assert.deepEqual([ui.sent.length, ui.timers.size], [1, 1]);
  // Falling asleep while following: the timer goes, the lines stay.
  ui.tool.mount(ui.host, record('Frozen'));
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.rows().length, 1);
  assert.match(ui.status(), /asleep.* 1 lines\.$/);
  ui.tool.mount(ui.host, record('Running'));
  assert.equal(ui.timers.size, 1);
  await ui.tick();
  assert.equal(ui.sent.length, 2);
  assert.notEqual(ui.sent[1].since, null);
});

test('an error stops following, is shown, and Retry continues from the same position', async () => {
  const answers = [report([line(1)]), new Error('Android logcat timed out'), report([line(2)]),
    new Error('This device is asleep. Open an app or the Android window, then retry.'), {runtime_id:OTHER}];
  const ui = panel(() => { const next = answers.shift(); if (next instanceof Error) throw next; return next; });
  ui.tool.mount(ui.host, record());
  await settle();
  await ui.tick();
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.problem().hidden, false);
  assert.equal(ui.problem().textContent, 'Could not read the logs: Android logcat timed out Use Retry to try again.');
  assert.deepEqual([ui.problem().className, ui.problem().attributes.role], ['runtime-feedback error', 'alert']);
  assert.equal(ui.button('Retry').hidden, false);
  ui.tool.mount(ui.host, record());             // a re-render does not retry by itself
  await settle();
  assert.deepEqual([ui.sent.length, ui.timers.size], [2, 0]);
  ui.button('Retry').click();
  await settle();
  assert.equal(ui.sent[2].since, ui.sent[1].since);
  assert.equal(ui.problem().hidden, true);
  assert.equal(ui.button('Retry').hidden, true);
  assert.equal(ui.rows().length, 2);
  // The controller's "asleep" answer is a calm message, still without polling.
  await ui.tick();
  assert.deepEqual([ui.problem().className, ui.problem().attributes.role, ui.timers.size], ['runtime-feedback', 'status', 0]);
  assert.match(ui.problem().textContent, /^This device is asleep/);
  // A new state clears it and following resumes. An answer for another device is an error.
  ui.tool.mount(ui.host, record('Frozen'));
  assert.equal(ui.problem().hidden, true);
  ui.tool.mount(ui.host, record('Running'));
  await ui.tick();
  assert.match(ui.problem().textContent, /Invalid log answer or another device/);
  assert.equal(ui.timers.size, 0);
});

test('Follow pauses and resumes without losing the position', async () => {
  const ui = panel((request, sent) => report([line(sent.length)]));
  ui.tool.mount(ui.host, record());
  await settle();
  const follow = ui.button('Follow');
  assert.deepEqual([follow.textContent, follow.attributes['aria-pressed']], ['Pause', 'true']);
  follow.click();
  assert.deepEqual([follow.textContent, follow.attributes['aria-pressed'], ui.timers.size], ['Follow', 'false', 0]);
  assert.match(ui.status(), /Paused\.$/);
  ui.tool.mount(ui.host, record());
  await settle();
  assert.deepEqual([ui.sent.length, ui.timers.size], [1, 0]);
  follow.click();                               // resuming reads at once, then keeps polling
  await settle();
  assert.equal(ui.sent.length, 2);
  assert.equal(ui.sent[1].since, `${line(1).time}000000:0123abcd`);
  assert.equal(ui.timers.size, 1);
});

test('level and package start a fresh read; the text filter only hides lines', async () => {
  const ui = panel((request, sent) => report([line(sent.length), line(sent.length, {level:'E', tag:'Crash', message:'boom', pid:42})],
    {note:request.package === 'com.example.gone' ? 'com.example.gone is not running. Open the app to see its log lines.' : null}));
  ui.tool.mount(ui.host, record());
  await settle();
  const [packageInput, filterInput] = ui.inputs();
  assert.deepEqual(ui.select().children.map(option => [option.value, option.textContent]),
    [['V', 'Verbose'], ['D', 'Debug'], ['I', 'Info'], ['W', 'Warning'], ['E', 'Error'], ['F', 'Fatal']]);
  ui.select().value = 'W'; ui.select().fire('change');
  assert.equal(ui.rows().length, 0);
  await settle();
  assert.deepEqual([ui.sent[1].level, ui.sent[1].since, ui.rows().length, ui.timers.size], ['W', null, 2, 1]);
  packageInput.value = ' com.example;rm '; packageInput.fire('change');
  await settle();
  assert.equal(ui.sent.length, 2);
  assert.equal(packageInput.attributes['aria-invalid'], 'true');
  assert.match(ui.toasts.at(-1)[1], /full app package name/);
  packageInput.value = ' com.example.gone '; packageInput.fire('change');
  await settle();
  assert.deepEqual([ui.sent[2].package, ui.sent[2].since, ui.sent[2].level], ['com.example.gone', null, 'W']);
  assert.match(ui.status(), /^com\.example\.gone is not running\./);
  assert.equal(ui.timers.size, 1);              // the app may start later
  packageInput.value = ''; packageInput.fire('change');
  await settle();
  assert.equal(ui.sent[3].package, null);
  filterInput.value = '  CRASH '; filterInput.fire('input');
  assert.equal(ui.sent.length, 4);
  assert.deepEqual(ui.rows().map(row => row.hidden), [true, false]);
  assert.equal(ui.status(), '1 of 2 lines shown.');
  await ui.tick();                              // new lines obey the filter too
  assert.deepEqual(ui.rows().map(row => row.hidden), [true, false, true, false]);
  filterInput.value = ''; filterInput.fire('input');
  assert.equal(ui.shown().length, 4);
});

test('the view keeps a bounded number of lines, marks gaps and follows the end only when the user is there', async () => {
  const batch = start => Array.from({length:1500}, (_, index) => line(start + index));
  const answers = [report(batch(0), {truncated:true}), report(batch(1500)), report(batch(3000), {truncated:true}), report([line(9000)])];
  const ui = panel(() => answers.shift());
  ui.tool.mount(ui.host, record());
  await settle();
  const view = ui.view();
  assert.equal(ui.rows().length, 1500);         // older lines existing before the first read are not a gap
  assert.equal(view.scrollTop, 0);
  view.scrollHeight = 30000; view.clientHeight = 300; view.scrollTop = 29700;
  await ui.tick();
  assert.equal(view.scrollTop, 30000);
  view.scrollHeight = 60000; view.scrollTop = 1000;   // the user scrolled up to read
  await ui.tick();
  assert.equal(view.scrollTop, 1000);
  assert.equal(ui.rows().length, Logs.MAX_KEPT);
  assert.match(ui.rows()[0].textContent, /message 501$/);
  assert.match(ui.rows().at(-1).textContent, /message 4499$/);
  const gaps = ui.rows().filter(row => row.className === 'log-line log-gap');
  assert.equal(gaps.length, 1);
  assert.match(gaps[0].textContent, /some lines were skipped/);
  assert.equal(ui.status(), '4000 lines.');
  ui.button('Clear').click();
  assert.equal(ui.rows().length, 0);
  assert.equal(ui.button('Copy').disabled && ui.button('Save…').disabled && ui.button('Clear').disabled, true);
  await ui.tick();                              // cleared lines do not come back
  assert.equal(ui.sent.at(-1).since, `${line(4499).time}000000:0123abcd`);
  assert.equal(ui.rows().length, 1);
});

test('Copy and Save take the lines that are shown, as plain text written by the desktop user', async () => {
  const ui = panel(() => report([line(1), line(2, {tag:'Other', message:'<img src=x onerror=alert(1)>'})]));
  ui.tool.mount(ui.host, record());
  await settle();
  assert.equal(ui.rows()[1].children.length, 0);          // text only, never markup
  assert.match(ui.rows()[1].textContent, /Other: <img src=x onerror=alert\(1\)>$/);
  const [, filterInput] = ui.inputs();
  filterInput.value = 'other'; filterInput.fire('input');
  await ui.button('Copy').click();
  assert.deepEqual(ui.copied, [ui.rows()[1].textContent + '\n']);
  assert.deepEqual(ui.toasts.at(-1), ['success', '1 log lines copied. They may contain personal data.']);
  await ui.button('Save…').click();
  assert.equal(ui.saved[0][0], 'dialog');
  assert.match(ui.saved[0][1].defaultPath, /^android-logs-Phone-\d{8}-\d{6}\.txt$/);
  assert.deepEqual(ui.saved[1], ['save_device_log', {path:'/home/user/logs.txt', content:ui.rows()[1].textContent + '\n'}]);
  assert.match(ui.toasts.at(-1)[1], /saved to \/home\/user\/logs\.txt/);
  assert.equal(ui.sent.every(request => request.op === 'logs_get'), true);   // saving never goes through the controller

  const refused = panel(() => report([line(1)]), {clipboard:{writeText:async () => { throw new Error('NotAllowedError'); }},
    dialog:{save:async () => null}});
  refused.tool.mount(refused.host, record());
  await settle();
  await refused.button('Copy').click();
  assert.deepEqual(refused.toasts.at(-1), ['error', 'Copying was not allowed. Use Save… to keep these lines in a file.']);
  await refused.button('Save…').click();          // cancelled dialog: nothing is written, nothing is said
  assert.equal(refused.saved.length, 0);
  assert.equal(refused.toasts.length, 1);
  const failing = panel(() => report([line(1)]), {invoke:async () => { throw 'Could not create the log file. Choose a new file name: File exists'; }});
  failing.tool.mount(failing.host, record());
  await settle();
  await failing.button('Save…').click();
  assert.deepEqual(failing.toasts.at(-1), ['error', 'Could not save the logs: Could not create the log file. Choose a new file name: File exists']);
});

test('the panel is wired into the page, the request layers agree and the UI warns about personal data', () => {
  const frontend = 'crates/anvildroid-gui/src-frontend/';
  const source = fs.readFileSync(frontend + 'runtime-logs.js', 'utf8');
  assert.match(source, /Logs may contain personal data/);
  assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML/);
  assert.match(fs.readFileSync(frontend + 'main.js', 'utf8'), /AnvilRuntimeLogs\.create\(deviceTool\)/);
  assert.match(fs.readFileSync(frontend + 'index.html', 'utf8'), /runtime-logs\.js/);
  const css = fs.readFileSync(frontend + 'layout.css', 'utf8');
  const block = css.slice(css.indexOf('/* Device settings → Logs'));
  assert.match(block, /\.runtime-logs \.logs-view \{[^}]*overflow: auto/);
  assert.doesNotMatch(block.split('\n').filter(row => row.includes('logs') || row.includes('log-')).join('\n'), /#[0-9a-fA-F]{3,8}\b|rgb\(/);
  const python = fs.readFileSync('services/runtime-logs.py', 'utf8');
  assert.match(python, /PARAMS = \{'lines', 'level', 'package', 'since'\}/);
  assert.match(python, /MIN_LINES, MAX_LINES = 50, 2000/);
  assert.ok(Logs.REQUEST_LINES >= 50 && Logs.REQUEST_LINES <= 2000);
  assert.match(fs.readFileSync('crates/anvildroid-core/src/controller.rs', 'utf8'),
    /LogsGet \{ id: String, lines: u32, level: String, package: Option<String>, since: Option<String> \}/);
  const rust = fs.readFileSync('crates/anvildroid-gui/src/main.rs', 'utf8');
  assert.match(rust, /mod device_log;/);
  assert.match(rust, /device_log::save_device_log,/);
});
