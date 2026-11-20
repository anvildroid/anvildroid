const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Capture = require('../crates/anvildroid-gui/src-frontend/runtime-capture.js');

const ID = 'r-' + '1'.repeat(32), OTHER = 'r-' + '2'.repeat(32);
const WHEN = new Date(2026, 10, 9, 7, 5, 3);
const shot = (extra = {}) => ({runtime_id:ID, bytes:812345, width:1984, height:1280, path:'/home/me/shot.png', ...extra});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };

function dom() {
  function node(tag) {
    return {tag, children:[], textContent:'', className:'', dataset:{}, attributes:{}, listeners:{}, disabled:false, parentNode:null,
      append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      click() { return this.listeners.click?.(); },
      all(match, found = []) { for (const child of this.children) { if (match(child)) found.push(child); child.all(match, found); } return found; },
      text() { return [this.textContent, ...this.children.map(child => child.text())].join(' '); },
    };
  }
  return {createElement:node};
}
function panel({choose = async () => '/home/me/shot.png', answer = async () => shot()} = {}) {
  const dialogs = [], invoked = [], toasts = [];
  const document = dom(), host = document.createElement('div');
  const tool = Capture.create({document, now:() => WHEN,
    dialog:{save:async options => { dialogs.push(options); return choose(options); }},
    invoke:async (command, args) => { invoked.push([command, args]); return answer(args); },
    toast:(message, kind) => toasts.push([kind, message]),
    request:async () => { throw new Error('the panel must not send controller requests itself'); }});
  const find = match => host.all(match)[0];
  return {tool, host, dialogs, invoked, toasts,
    button:() => find(item => item.tag === 'button'),
    status:() => find(item => item.attributes.role === 'status'),
    waiting:() => find(item => item.className.includes('capture-waiting'))};
}
const device = (extra = {}) => ({id:ID, name:'Gaming phone', state:'Running', ...extra});

test('default file name carries the device name and local time and is safe for file systems', () => {
  assert.equal(Capture.defaultFileName('Gaming phone', WHEN), 'AnvilDroid Gaming phone 2026-11-09 07.05.03.png');
  assert.equal(Capture.defaultFileName('System Waydroid', new Date(2026, 0, 31, 23, 59, 59)), 'AnvilDroid System Waydroid 2026-01-31 23.59.59.png');
  assert.equal(Capture.safeName('../../etc/passwd'), 'etc passwd');
  assert.equal(Capture.safeName('a<b>c:d"e/f\\g|h?i*j'), 'a b c d e f g h i j');
  assert.equal(Capture.safeName('  tab\there\nnew\u0000line  '), 'tab here new line');
  assert.equal(Capture.safeName('...'), 'device');
  assert.equal(Capture.safeName(''), 'device');
  assert.equal(Capture.safeName(undefined), 'device');
  assert.equal(Capture.safeName('Điện thoại 手机'), 'Điện thoại 手机');
  assert.equal(Capture.safeName('x'.repeat(200)).length, 60);
  for (const name of ['../x', 'a/b', 'c\\d', 'CON:', '\u0007bell', ' . '])
    assert.doesNotMatch(Capture.defaultFileName(name, WHEN), /[\u0000-\u001f<>:"/\\|?*]|^\.|\.\./);
});

test('results are checked before success is shown', () => {
  assert.equal(Capture.validate(shot(), ID).width, 1984);
  for (const bad of [null, {}, shot({runtime_id:OTHER}), shot({bytes:0}), shot({bytes:'812345'}), shot({width:0}), shot({height:-1}),
      shot({width:1e9}), shot({height:1.5}), shot({width:undefined})])
    assert.throws(() => Capture.validate(bad, ID), /did not confirm a screenshot of this device/);
});

test('one primary button, an explanation and a status line; nothing about recording', () => {
  const view = panel();
  view.tool.mount(view.host, device());
  const buttons = view.host.all(item => item.tag === 'button');
  assert.equal(buttons.length, 1);
  assert.deepEqual([buttons[0].textContent, buttons[0].className, buttons[0].type, buttons[0].disabled], ['Save screenshot…', 'btn btn-primary', 'button', false]);
  assert.equal(view.status().attributes['aria-live'], 'polite');
  assert.match(view.host.text(), /whole Android display/);
  assert.match(view.host.text(), /separate app windows.*Android’s own desktop arrangement.*can differ from the windows on your Linux desktop/);
  assert.equal(view.waiting().textContent, '');
  const source = fs.readFileSync('crates/anvildroid-gui/src-frontend/runtime-capture.js', 'utf8');
  assert.doesNotMatch(source, /record|video/i);
  assert.doesNotMatch(view.host.text(), /runtime/i);
  assert.doesNotMatch(source, /innerHTML/);
  assert.doesNotMatch(view.host.text(), /[—;]/);
});

test('saving asks for a file, sends only the device and the path, and reports the size', async () => {
  const view = panel();
  view.tool.mount(view.host, device());
  await view.button().click();
  assert.deepEqual(view.dialogs, [{defaultPath:'AnvilDroid Gaming phone 2026-11-09 07.05.03.png', title:'Save screenshot',
    filters:[{name:'PNG image', extensions:['png']}]}]);
  assert.deepEqual(view.invoked, [['save_device_screenshot', {id:ID, path:'/home/me/shot.png'}]]);
  assert.equal(view.status().textContent, 'Saved 1984 × 1280 screenshot to /home/me/shot.png');
  assert.equal(view.status().className, '');
  assert.deepEqual(view.toasts, [['success', 'Screenshot saved.']]);
  assert.deepEqual([view.button().disabled, view.button().textContent], [false, 'Save screenshot…']);
});

test('the button is busy while the dialog and the save are open', async () => {
  const chosen = deferred(), saved = deferred();
  const view = panel({choose:() => chosen.promise, answer:() => saved.promise});
  view.tool.mount(view.host, device());
  view.button().click();
  await settle();
  assert.deepEqual([view.button().disabled, view.button().textContent], [true, 'Saving…']);
  view.button().click();                           // a second click does nothing
  view.tool.mount(view.host, device());            // a re-render keeps the busy state
  await settle();
  assert.equal(view.dialogs.length, 1);
  assert.equal(view.button().disabled, true);
  chosen.resolve('/home/me/a.png');
  await settle();
  assert.equal(view.status().textContent, 'Taking the screenshot…');
  assert.equal(view.button().disabled, true);
  saved.resolve(shot());
  await settle();
  assert.equal(view.button().disabled, false);
  assert.match(view.status().textContent, /^Saved 1984 × 1280 screenshot to \/home\/me\/a\.png$/);
});

test('cancelling the dialog saves nothing and says nothing', async () => {
  const view = panel({choose:async () => null});
  view.tool.mount(view.host, device());
  await view.button().click();
  assert.deepEqual(view.invoked, []);
  assert.equal(view.status().textContent, '');
  assert.deepEqual(view.toasts, []);
  assert.equal(view.button().disabled, false);
});

test('a failed save shows the reason and allows another try', async () => {
  const answers = [() => { throw new Error('This device is asleep. Open an app or the Android window, then retry.'); },
    () => shot({runtime_id:OTHER}), () => { throw 'Runtime controller unavailable'; }, () => shot()];
  const view = panel({answer:async () => answers.shift()()});
  view.tool.mount(view.host, device());
  await view.button().click();
  assert.equal(view.status().textContent, 'The screenshot was not saved. This device is asleep. Open an app or the Android window, then retry.');
  assert.equal(view.status().className, 'runtime-feedback error');
  assert.equal(view.button().disabled, false);
  await view.button().click();
  assert.match(view.status().textContent, /not saved\. The controller did not confirm a screenshot of this device\./);
  await view.button().click();
  assert.match(view.status().textContent, /not saved\. Runtime controller unavailable/);
  assert.deepEqual(view.toasts, []);
  await view.button().click();
  assert.match(view.status().textContent, /^Saved /);
  assert.equal(view.status().className, '');
});

test('a device that is not running gets a calm hint and a disabled button', async () => {
  const view = panel();
  for (const [state, hint] of [['Stopped', 'Start this device to save a screenshot.'], ['Prepared', 'Start this device to save a screenshot.'],
      ['Frozen', 'This device is asleep. Open an app or the Android window, then save a screenshot.'],
      ['Starting', 'This device is starting. You can save a screenshot when it is running.']]) {
    view.tool.mount(view.host, device({state}));
    assert.equal(view.button().disabled, true, state);
    assert.equal(view.waiting().textContent, hint);
    assert.equal(view.waiting().className, 'hint capture-waiting');
    await view.button().click();
  }
  assert.deepEqual([view.dialogs, view.invoked], [[], []]);
  assert.equal(view.status().textContent, '');
  view.tool.mount(view.host, device());
  assert.deepEqual([view.button().disabled, view.waiting().textContent], [false, '']);
  view.tool.mount(view.host, device({id:'default', name:'System Waydroid', state:'Running'}));
  await view.button().click();
  assert.equal(view.dialogs[0].defaultPath, 'AnvilDroid System Waydroid 2026-11-09 07.05.03.png');
  assert.deepEqual(view.invoked[0], ['save_device_screenshot', {id:'default', path:'/home/me/shot.png'}]);
});

test('switching devices while the dialog is open saves nothing', async () => {
  const chosen = deferred();
  const view = panel({choose:() => chosen.promise});
  view.tool.mount(view.host, device());
  view.button().click();
  await settle();
  view.tool.mount(view.host, device({id:OTHER, name:'Work tablet'}));
  assert.deepEqual([view.button().disabled, view.button().textContent], [false, 'Save screenshot…']);
  chosen.resolve('/home/me/first.png');
  await settle();
  assert.deepEqual(view.invoked, []);
  assert.equal(view.status().textContent, '');
  assert.equal(view.button().disabled, false);
});

test('a result that arrives after a device change or after closing is not shown', async () => {
  for (const leave of [view => view.tool.mount(view.host, device({id:OTHER, name:'Work tablet'})), view => view.tool.deactivate()]) {
    for (const finish of [saved => saved.resolve(shot()), saved => saved.reject(new Error('Start this device first.'))]) {
      const saved = deferred();
      const view = panel({answer:() => saved.promise});
      view.tool.mount(view.host, device());
      const status = view.status(), button = view.button();
      button.click();
      await settle();
      assert.equal(status.textContent, 'Taking the screenshot…');
      leave(view);
      assert.equal(status.textContent, '');
      finish(saved);
      await settle();
      assert.equal(status.textContent, '');
      assert.equal(status.className, '');
      assert.deepEqual(view.toasts, []);
      assert.equal(button.textContent, 'Save screenshot…');
    }
  }
});

test('re-rendering the same device keeps the result and closing removes the panel', async () => {
  const view = panel();
  view.tool.mount(view.host, device());
  await view.button().click();
  const first = view.host.children[0];
  view.tool.mount(view.host, device({state:'Frozen'}));
  assert.equal(view.host.children.length, 1);
  assert.equal(view.host.children[0], first);
  assert.match(view.status().textContent, /^Saved /);
  assert.equal(view.button().disabled, true);
  const other = dom().createElement('div');
  view.tool.mount(other, device());
  assert.equal(other.children[0], first);
  view.tool.deactivate();
  assert.equal(other.children.length, 0);
  assert.equal(first.all(item => item.tag === 'button')[0].disabled, true);
  view.tool.deactivate();                          // closing twice is harmless
  view.tool.mount(view.host, device());
  assert.equal(view.status().textContent, '');
  assert.equal(view.button().disabled, false);
});

test('the command, styles and section the panel relies on exist', () => {
  const rust = fs.readFileSync('crates/anvildroid-gui/src/main.rs', 'utf8');
  assert.match(rust, /async fn save_device_screenshot\(id: String, path: String\)/);
  assert.match(rust.slice(rust.indexOf('generate_handler!')), /\bsave_device_screenshot,/);
  const css = fs.readFileSync('crates/anvildroid-gui/src-frontend/layout.css', 'utf8');
  const block = css.slice(css.indexOf('/* Device settings → Screenshot */'));
  assert.match(block, /\.capture-waiting:empty \{ display: none; \}/);
  assert.doesNotMatch(block.split('\n/*')[1] ?? block, /#[0-9a-f]{3,8}\b|rgb\(/i);
  const main = fs.readFileSync('crates/anvildroid-gui/src-frontend/main.js', 'utf8');
  assert.match(main, /<summary>Screenshot<\/summary><div class="runtime-capture-panel"><div data-runtime-capture=/);
});
