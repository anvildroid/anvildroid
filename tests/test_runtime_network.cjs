const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Network = require('../crates/anvildroid-gui/src-frontend/runtime-network.js');

const ID = 'r-' + '1'.repeat(32), OTHER = 'r-' + '2'.repeat(32);
const report = (extra = {}) => ({runtime_id:ID, proxy:null, dns:{mode:'automatic', hostname:''}, error:null, ...extra});
const proxied = (extra = {}) => report({proxy:{host:'192.168.1.10', port:3128, bypass:['localhost', '*.lan']}, ...extra});
const device = (extra = {}) => ({id:ID, name:'Phone', state:'Running', ...extra});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
const form = (extra = {}) => ({proxy:true, host:'192.168.1.10', port:'3128', bypass:'', mode:'automatic', hostname:'', ...extra});

function dom() {
  function node(tag) {
    return {tag, children:[], textContent:'', className:'', dataset:{}, attributes:{}, listeners:{}, disabled:false, hidden:false,
      value:'', checked:false, parentNode:null,
      append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      click() { return this.listeners.click?.(); },
      enter(text) { this.value = text; this.listeners.input?.(); },
      choose(value) { if (this.tag === 'select') this.value = value; else this.checked = value; this.listeners.change?.(); },
      all(match, found = []) { for (const child of this.children) { if (match(child)) found.push(child); child.all(match, found); } return found; },
      text() { return this.hidden ? '' : [this.textContent, ...this.children.map(child => child.text())].join(' '); },
    };
  }
  return {createElement:node};
}
function panel(handler) {
  const sent = [], document = dom(), host = document.createElement('div');
  const tool = Network.create({document, request:async request => { sent.push(request); return handler(request, sent); }});
  const labelled = label => host.all(item => item.tag === 'label' && item.children[0]?.textContent === label)[0].children[1];
  const ui = {
    get use() { return host.all(item => item.type === 'checkbox')[0]; },
    get host() { return labelled('Proxy host'); }, get port() { return labelled('Port'); },
    get bypass() { return labelled('No proxy for (comma separated)'); },
    get mode() { return labelled('Private DNS mode'); }, get dns() { return labelled('Provider hostname'); },
    get apply() { return host.all(item => item.tag === 'button' && item.className.includes('btn-primary'))[0]; },
    get reset() { return host.all(item => item.tag === 'button' && item.textContent === 'Reset')[0]; },
    get status() { return host.all(item => item.attributes.role === 'status')[0]; },
    get problem() { return host.all(item => item.attributes.role === 'alert')[0]; },
    get warning() { return host.all(item => item.className === 'runtime-feedback warning')[0]; },
    get blocked() { return host.all(item => item.className.includes('network-blocked'))[0]; },
  };
  return {tool, host, sent, ui};
}

test('the form is checked with the same rules as the controller', () => {
  assert.deepEqual(Network.check(form({host:' proxy.example.com ', port:' 8080 ', bypass:'localhost, *.lan ,10.0.0.1,', mode:'hostname', hostname:' dns.adguard.com '})),
    {problem:'', fields:{proxy_host:'proxy.example.com', proxy_port:8080, proxy_bypass:['localhost', '*.lan', '10.0.0.1'], dns_mode:'hostname', dns_hostname:'dns.adguard.com'}});
  // No proxy is always sent in full: empty host, port 0, no bypass. Typed leftovers are ignored.
  assert.deepEqual(Network.check(form({proxy:false, host:'-bad', port:'x', bypass:'--', mode:'off', hostname:'ignored'})).fields,
    {proxy_host:'', proxy_port:0, proxy_bypass:[], dns_mode:'off', dns_hostname:''});
  for (const host of ['proxy.example.com', 'localhost', '127.0.0.1', '10.0.0.1', 'a-b.example'])
    assert.equal(Network.check(form({host})).problem, '', host);
  for (const host of ['', '   ', '-proxy', '--help', 'proxy-', 'pro xy', 'a,b', 'a"b', "a'b", 'a_b', 'a..b', 'xn--abc.example', 'user@host',
      'host:8080', 'http://proxy', '*.example.com', '::1', '[::1]', '256.1.1.1', '1.2.3', '01.2.3.4', 'host.123', 'null', 'a'.repeat(64), 'a.'.repeat(127) + 'a', 'prøxy', 'proxy\nx'])
    assert.equal(Network.check(form({host})).field, 'host', JSON.stringify(host));
  assert.match(Network.check(form({host:'fe80::1'})).problem, /IPv6 addresses are not supported/);
  for (const port of ['1', '80', '65535']) assert.equal(Network.check(form({port})).problem, '', port);
  for (const port of ['', '0', '65536', '-1', '80.0', '8e3', '0x50', 'http', '123456', '8 0'])
    assert.deepEqual(Network.check(form({port})), {problem:'Proxy port must be a number from 1 to 65535.', field:'port'}, port);
  for (const bypass of ['-x', '*', '*example.com', 'a.*.com', '*.10.0.0.1', 'a b', 'a:1', '::1', '10.0.0', 'a, A', 'null',
      Array.from({length:33}, (_, index) => `h${index}.lan`).join(',')])
    assert.equal(Network.check(form({bypass})).field, 'bypass', bypass);
  assert.equal(Network.check(form({bypass:Array.from({length:32}, (_, index) => `h${index}.lan`).join(',')})).problem, '');
  for (const hostname of ['dns.adguard.com', 'one.one.one.one', 'xn--dns-abc.example'])
    assert.equal(Network.check(form({mode:'hostname', hostname})).problem, '', hostname);
  for (const hostname of ['', '-dns.example', 'dns_x.example', 'dns example', 'dns.example.', '1.1.1.1', '2606:4700::1111', 'https://dns.example', 'null', 'a'.repeat(64)])
    assert.equal(Network.check(form({mode:'hostname', hostname})).field, 'hostname', hostname);
  assert.equal(Network.check(form({mode:'strict'})).field, 'mode');
  // The controller keeps the same patterns and limits.
  const python = fs.readFileSync('services/runtime-proxy.py', 'utf8');
  for (const text of ["LABEL = r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*'", "DNS_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'", 'MAX_NAME = 253', 'MAX_BYPASS = 32'])
    assert.ok(python.includes(text), text);
});

test('answers are validated before they are shown', () => {
  assert.equal(Network.validate(proxied(), ID).proxy.port, 3128);
  assert.equal(Network.validate(report({error:'The network settings may be partly applied.'}), ID).proxy, null);
  for (const bad of [null, {}, report({runtime_id:OTHER}), report({dns:null}), report({dns:{mode:'strict', hostname:''}}), report({dns:{mode:'off'}}),
      report({error:undefined}), report({error:5}), report({proxy:'p:1'}), report({proxy:{host:'', port:1, bypass:[]}}),
      report({proxy:{host:'p', port:'1', bypass:[]}}), report({proxy:{host:'p', port:0, bypass:[]}}), report({proxy:{host:'p', port:1}}),
      report({proxy:{host:'p', port:1, bypass:[5]}}), report({proxy:{host:'x'.repeat(300), port:1, bypass:[]}})])
    assert.throws(() => Network.validate(bad, ID), /Invalid network settings response/);
});

test('localhost and 127.x point at Android itself', () => {
  for (const host of ['localhost', 'LOCALHOST', ' localhost ', '127.0.0.1', '127.1.2.3']) assert.equal(Network.isLoopback(host), true, host);
  for (const host of ['', 'localhost.example', '192.168.1.10', '127.example', '1127.0.0.1', '127.0.0']) assert.equal(Network.isLoopback(host), false, host);
});

test('settings load once per opened section and fill the form', async () => {
  const {tool, host, sent, ui} = panel(() => proxied({dns:{mode:'hostname', hostname:'dns.adguard.com'}}));
  await tool.mount(host, device());
  assert.deepEqual(sent, [{op:'network_get', id:ID}]);
  assert.deepEqual([ui.use.checked, ui.host.value, ui.port.value, ui.bypass.value, ui.mode.value, ui.dns.value],
    [true, '192.168.1.10', '3128', 'localhost, *.lan', 'hostname', 'dns.adguard.com']);
  assert.equal(ui.apply.disabled, false);
  assert.equal(ui.blocked.hidden, true);
  assert.equal(ui.bypass.parentNode.parentNode.dataset.level, 'advanced');
  assert.equal(ui.status.attributes.role, 'status');
  // The page re-renders often: same device, no new request, typed text is kept.
  ui.host.enter('10.0.0.2');
  for (let i = 0; i < 3; i++) await tool.mount(host, device());
  assert.equal(sent.length, 1);
  assert.equal(ui.host.value, '10.0.0.2');
  assert.equal(host.children.length, 1);
  // Reset discards the edit and reads the device again.
  await ui.reset.click();
  assert.equal(sent.length, 2);
  assert.equal(ui.host.value, '192.168.1.10');
  // Closing and reopening the section reads again. Another device too.
  tool.deactivate();
  assert.equal(host.children.length, 0);
  await tool.mount(host, device());
  assert.equal(sent.length, 3);
  await tool.mount(host, device({id:OTHER})).catch(() => {});
  assert.deepEqual(sent.at(-1), {op:'network_get', id:OTHER});
});

test('a failed load is shown once and Reset retries', async () => {
  let fail = true;
  const {tool, host, sent, ui} = panel(() => { if (fail) throw new Error('Stop and start this device once to load the updated device tools.'); return report(); });
  await tool.mount(host, device());
  assert.match(ui.status.textContent, /Could not read the network settings: Stop and start this device once.* Use Reset to retry\./);
  assert.equal(ui.status.className, 'runtime-feedback error');
  assert.deepEqual([ui.apply.disabled, ui.use.disabled, ui.reset.disabled], [true, true, false]);
  await tool.mount(host, device());
  assert.equal(sent.length, 1);
  fail = false;
  await ui.reset.click();
  assert.deepEqual([sent.length, ui.status.textContent, ui.status.className, ui.apply.disabled], [2, '', '', false]);
});

test('a device that is not running shows a calm hint and asks nothing', async () => {
  const {tool, host, sent, ui} = panel(() => report());
  await tool.mount(host, device({state:'Stopped'}));
  assert.equal(sent.length, 0);
  assert.deepEqual([ui.blocked.hidden, ui.blocked.textContent], [false, 'Start this device to change its network settings.']);
  assert.equal(ui.blocked.className.includes('error'), false);
  for (const control of [ui.use, ui.host, ui.port, ui.bypass, ui.mode, ui.dns, ui.apply, ui.reset]) assert.equal(control.disabled, true);
  await ui.apply.click(); await ui.reset.click();
  assert.equal(sent.length, 0);
  await tool.mount(host, device({state:'Frozen'}));
  assert.match(ui.blocked.textContent, /This device is asleep\./);
  assert.equal(sent.length, 0);
  // Started: read once. Asleep again: disabled, typed text kept. Awake again: read again.
  await tool.mount(host, device());
  assert.deepEqual([sent.length, ui.blocked.hidden, ui.apply.disabled], [1, true, false]);
  ui.use.choose(true); ui.host.enter('10.0.0.2');
  await tool.mount(host, device({state:'Frozen'}));
  assert.deepEqual([ui.apply.disabled, ui.host.disabled, ui.host.value, ui.blocked.hidden], [true, true, '10.0.0.2', false]);
  await tool.mount(host, device());
  assert.equal(sent.length, 2);
});

test('fields follow the proxy switch and the DNS mode, with a warning for localhost', async () => {
  const {tool, host, ui} = panel(() => report());
  await tool.mount(host, device());
  assert.deepEqual([ui.host.disabled, ui.host.parentNode.parentNode.parentNode.hidden, ui.dns.disabled, ui.dns.parentNode.parentNode.hidden], [true, true, true, true]);
  ui.use.choose(true);
  assert.deepEqual([ui.host.disabled, ui.port.disabled, ui.bypass.disabled, ui.host.parentNode.parentNode.parentNode.hidden], [false, false, false, false]);
  assert.equal(ui.warning.hidden, true);
  for (const name of ['localhost', '127.0.0.1']) {
    ui.host.enter(name);
    assert.equal(ui.warning.hidden, false, name);
    assert.match(ui.warning.textContent, /this Android device itself, not your computer.*LAN address/);
  }
  ui.host.enter('192.168.1.10');
  assert.equal(ui.warning.hidden, true);
  ui.mode.choose('hostname');
  assert.deepEqual([ui.dns.disabled, ui.dns.parentNode.parentNode.hidden], [false, false]);
  ui.mode.choose('off');
  assert.equal(ui.dns.disabled, true);
  // The limits of Android's global proxy are stated, and there are no credential fields.
  const text = host.text();
  assert.match(text, /HTTP and HTTPS traffic only\. Some apps ignore it\. SOCKS proxies.*VPNs are not supported/);
  assert.match(text, /lost when you reinstall this device or clear its data/);
  assert.equal(host.all(item => item.tag === 'input').length, 5);
  assert.equal(host.all(item => /password|user/i.test(item.placeholder || '') || item.type === 'password').length, 0);
});

test('Apply validates first, then sends every field and shows what Android reports', async () => {
  let release;
  const {tool, host, sent, ui} = panel(request => request.op === 'network_get' ? report()
    : new Promise(resolve => { release = () => resolve(proxied({proxy:{host:'192.168.1.10', port:3128, bypass:['*.lan']}})); }));
  await tool.mount(host, device());
  ui.use.choose(true); ui.host.enter('192.168.1.10'); ui.port.enter('99999');
  await ui.apply.click();
  assert.equal(sent.length, 1);
  assert.deepEqual([ui.problem.hidden, ui.problem.textContent], [false, 'Proxy port must be a number from 1 to 65535.']);
  ui.port.enter('3128');
  assert.equal(ui.problem.hidden, true);
  ui.host.enter('my proxy');
  await ui.apply.click();
  assert.match(ui.problem.textContent, /must be a host name or an IPv4 address/);
  ui.host.enter('192.168.1.10'); ui.bypass.enter('*.lan, LOCALHOST,');
  ui.mode.choose('hostname');
  await ui.apply.click();
  assert.match(ui.problem.textContent, /Enter the hostname of the Private DNS provider/);
  assert.equal(sent.length, 1);
  ui.mode.choose('automatic');
  const pending = ui.apply.click();
  assert.deepEqual(sent.at(-1), {op:'network_set', id:ID, proxy_host:'192.168.1.10', proxy_port:3128, proxy_bypass:['*.lan', 'LOCALHOST'],
    dns_mode:'automatic', dns_hostname:''});
  // Busy: nothing can be changed or sent twice.
  assert.deepEqual([ui.apply.disabled, ui.apply.textContent, ui.reset.disabled, ui.host.disabled, ui.use.disabled], [true, 'Applying…', true, true, true]);
  await ui.apply.click(); await ui.reset.click();
  await tool.mount(host, device());
  assert.equal(sent.length, 2);
  release(); await pending;
  assert.deepEqual([ui.apply.disabled, ui.apply.textContent, ui.bypass.value], [false, 'Apply', '*.lan']);
  assert.match(ui.status.textContent, /^Network settings applied\. The bypass list takes effect the next time this device starts\.$/);
  assert.equal(ui.status.className, 'runtime-feedback success');
});

test('turning the proxy off sends the empty proxy', async () => {
  const {tool, host, sent, ui} = panel(request => request.op === 'network_get' ? proxied() : report({dns:{mode:'off', hostname:''}}));
  await tool.mount(host, device());
  ui.use.choose(false); ui.mode.choose('off');
  await ui.apply.click();
  assert.deepEqual(sent.at(-1), {op:'network_set', id:ID, proxy_host:'', proxy_port:0, proxy_bypass:[], dns_mode:'off', dns_hostname:''});
  assert.deepEqual([ui.use.checked, ui.host.value, ui.mode.value, ui.status.textContent], [false, '', 'off', 'Network settings applied.']);
});

test('a partial failure shows the error and the real state; a refusal keeps the form', async () => {
  const partly = 'The network settings may be partly applied. Android settings failed: Failed transaction';
  let answer = () => proxied({proxy:{host:'192.168.1.10', port:3128, bypass:[]}, error:partly});
  const {tool, host, ui} = panel(request => request.op === 'network_get' ? report() : answer());
  await tool.mount(host, device());
  ui.use.choose(true); ui.host.enter('192.168.1.10'); ui.port.enter('3128'); ui.bypass.enter('a.lan');
  await ui.apply.click();
  assert.equal(ui.status.textContent, partly + '. The form shows what this device uses now.');
  assert.equal(ui.status.className, 'runtime-feedback error');
  assert.deepEqual([ui.bypass.value, ui.apply.disabled], ['', false]);
  answer = () => { throw new Error('This device is asleep. Open an app or the Android window, then retry.'); };
  ui.bypass.enter('b.lan');
  await ui.apply.click();
  assert.match(ui.status.textContent, /^Could not apply the network settings: This device is asleep\./);
  assert.deepEqual([ui.bypass.value, ui.apply.disabled, ui.apply.textContent], ['b.lan', false, 'Apply']);
  answer = () => report({runtime_id:OTHER});
  await ui.apply.click();
  assert.match(ui.status.textContent, /Invalid network settings response/);
});

test('an answer that arrives after the section closed or the device changed is dropped', async () => {
  const waiting = [];
  const {tool, host, sent, ui} = panel(request => request.op === 'network_get' ? report({runtime_id:request.id})
    : new Promise(resolve => waiting.push(() => resolve(proxied()))));
  await tool.mount(host, device());
  ui.use.choose(true); ui.host.enter('192.168.1.10'); ui.port.enter('3128');
  const pending = ui.apply.click();
  tool.deactivate();
  await tool.mount(host, device({id:OTHER}));
  waiting[0](); await pending; await settle();
  assert.equal(host.children[0].dataset.runtimeId, OTHER);
  assert.deepEqual([ui.use.checked, ui.host.value, ui.status.textContent, ui.apply.disabled, ui.apply.textContent], [false, '', '', false, 'Apply']);
  assert.deepEqual(sent.map(item => [item.op, item.id]), [['network_get', ID], ['network_set', ID], ['network_get', OTHER]]);
});

test('the panel is wired into the page and styled with tokens only', () => {
  const front = 'crates/anvildroid-gui/src-frontend/';
  assert.match(fs.readFileSync(front + 'main.js', 'utf8'), /AnvilRuntimeNetwork\.create\(/);
  assert.match(fs.readFileSync(front + 'index.html', 'utf8'), /runtime-network\.js/);
  const css = fs.readFileSync(front + 'layout.css', 'utf8');
  const block = css.slice(css.indexOf('/* Device settings > Network'));
  const own = block.split('\n').filter(line => line.startsWith('.runtime-network'));
  assert.ok(own.length >= 8);
  for (const line of own) assert.doesNotMatch(line, /#[0-9a-fA-F]{3,8}\b|rgb\(/, line);
  const source = fs.readFileSync(front + 'runtime-network.js', 'utf8');
  assert.doesNotMatch(source, /innerHTML/);
  assert.doesNotMatch(source, /—/);
});
