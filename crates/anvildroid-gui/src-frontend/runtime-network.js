// Network section of Device settings: HTTP proxy and Private DNS of one device.
// Android keeps both in its own settings (services/runtime-proxy.py); this file
// shows what Android reports, checks the form and sends one request per Apply.
(function(root) {
  const DNS_MODES = ['off', 'automatic', 'hostname'];
  const MAX_NAME = 253, MAX_BYPASS = 32;
  // The same rules as the controller, so a mistake is explained before anything is sent.
  const LABEL = '[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*';
  const HOST_NAME = new RegExp(`^${LABEL}(?:\\.${LABEL})*$`);
  const DNS_LABEL = '[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?';
  const DNS_NAME = new RegExp(`^${DNS_LABEL}(?:\\.${DNS_LABEL})*$`);
  const STOPPED = 'Start this device to change its network settings.';
  const ASLEEP = 'This device is asleep. Open an app or the Android window to change its network settings.';
  const LOOPBACK = 'localhost and 127.0.0.1 mean this Android device itself, not your computer. For a proxy that runs on this computer, enter the computer’s LAN address, for example 192.168.1.10.';
  const BYPASS_LATER = 'The bypass list takes effect the next time this device starts.';

  function ipv4(value) {
    const parts = value.split('.');
    return parts.length === 4 && parts.every(part => /^(0|[1-9][0-9]{0,2})$/.test(part) && Number(part) <= 255);
  }
  const endsInNumber = value => /^[0-9]+$/.test(value.split('.').pop());
  const isLoopback = value => /^localhost$/i.test(value.trim()) || (ipv4(value.trim()) && value.trim().startsWith('127.'));

  // A problem with a host name or IPv4 address, or '' when it is fine.
  function nameProblem(value, what, example) {
    const title = what[0].toUpperCase() + what.slice(1);
    if (!value) return `Enter ${what}.`;
    if (value.length > MAX_NAME) return `${title} is too long. Use at most ${MAX_NAME} characters.`;
    if (value.includes(':') || value.includes('[')) return `IPv6 addresses are not supported for ${what}. Use a host name or an IPv4 address.`;
    if (!HOST_NAME.test(value) || value.split('.').some(label => label.length > 63))
      return `${title} must be a host name or an IPv4 address, for example ${example}.`;
    if (endsInNumber(value) && !ipv4(value)) return `${title} is not a valid IPv4 address.`;
    if (value.toLowerCase() === 'null') return `${title} cannot be "null".`;
    return '';
  }
  function bypassProblem(entries) {
    if (entries.length > MAX_BYPASS) return `The bypass list can hold at most ${MAX_BYPASS} entries.`;
    const seen = new Set();
    for (const entry of entries) {
      const wildcard = entry.startsWith('*.');
      const problem = nameProblem(wildcard ? entry.slice(2) : entry, 'a bypass entry', 'example.com, *.example.com or 192.168.1.10');
      if (problem) return `${problem} Check "${entry.slice(0, 40)}".`;
      if (wildcard && ipv4(entry.slice(2))) return 'A bypass wildcard needs a domain name, for example *.example.com.';
      if (seen.has(entry.toLowerCase())) return `The bypass list has "${entry.slice(0, 40)}" twice.`;
      seen.add(entry.toLowerCase());
    }
    return '';
  }
  function dnsProblem(hostname) {
    if (!hostname) return 'Enter the hostname of the Private DNS provider.';
    if (hostname.length > MAX_NAME) return `The Private DNS hostname is too long. Use at most ${MAX_NAME} characters.`;
    if (!DNS_NAME.test(hostname) || endsInNumber(hostname) || hostname.toLowerCase() === 'null')
      return 'Private DNS needs a provider hostname such as dns.adguard.com, not an address.';
    return '';
  }
  const parseBypass = text => String(text).split(',').map(entry => entry.trim()).filter(Boolean);

  // Check what the form holds. Returns {problem, field} or {fields} for the network_set request.
  function check({proxy, host, port, bypass, mode, hostname}) {
    const fields = {proxy_host:'', proxy_port:0, proxy_bypass:[], dns_mode:mode, dns_hostname:''};
    if (proxy) {
      const name = String(host).trim(), digits = String(port).trim(), entries = parseBypass(bypass);
      let problem = nameProblem(name, 'the proxy host', 'proxy.example.com or 192.168.1.10');
      if (problem) return {problem, field:'host'};
      if (!/^[0-9]{1,5}$/.test(digits) || Number(digits) < 1 || Number(digits) > 65535)
        return {problem:'Proxy port must be a number from 1 to 65535.', field:'port'};
      problem = bypassProblem(entries);
      if (problem) return {problem, field:'bypass'};
      Object.assign(fields, {proxy_host:name, proxy_port:Number(digits), proxy_bypass:entries});
    }
    if (!DNS_MODES.includes(mode)) return {problem:'Choose a Private DNS mode.', field:'mode'};
    if (mode === 'hostname') {
      const name = String(hostname).trim(), problem = dnsProblem(name);
      if (problem) return {problem, field:'hostname'};
      fields.dns_hostname = name;
    }
    return {problem:'', fields};
  }

  function validate(value, id) {
    const bad = () => { throw new Error('Invalid network settings response or another device. Use Reset to read them again.'); };
    const text = (item, max) => typeof item === 'string' && item.length <= max;
    if (!value || value.runtime_id !== id || !value.dns || !DNS_MODES.includes(value.dns.mode) || !text(value.dns.hostname, MAX_NAME) ||
        (value.error !== null && !text(value.error, 2000))) bad();
    const proxy = value.proxy;
    if (proxy !== null && (!proxy || !text(proxy.host, MAX_NAME) || !proxy.host || !Number.isInteger(proxy.port) || proxy.port < 1 ||
        proxy.port > 65535 || !Array.isArray(proxy.bypass) || proxy.bypass.length > 64 || !proxy.bypass.every(item => text(item, MAX_NAME)))) bad();
    return value;
  }

  function create({request, document}) {
    function element(tag, text, parent, className) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      if (parent) parent.append(node);
      return node;
    }
    function field(parent, label, placeholder) {
      const wrap = element('label', undefined, parent, 'network-field');
      element('span', label, wrap);
      const input = element('input', undefined, wrap);
      input.type = 'text'; input.placeholder = placeholder; input.autocomplete = 'off'; input.spellcheck = false;
      input.setAttribute('autocapitalize', 'none');
      return input;
    }
    const panel = element('section', undefined, null, 'runtime-inline-feature runtime-network');
    element('p', 'Android keeps these settings in its own data. They survive restarts. They are lost when you reinstall this device or clear its data.', panel, 'hint');
    const blocked = element('p', STOPPED, panel, 'hint network-blocked');
    const form = element('div', undefined, panel, 'network-form');

    element('h4', 'HTTP proxy', form);
    const useLabel = element('label', undefined, form);
    const useProxy = element('input', undefined, useLabel); useProxy.type = 'checkbox';
    element('span', 'Send web traffic through an HTTP proxy', useLabel);
    const proxyBox = element('div', undefined, form, 'network-proxy');
    const address = element('div', undefined, proxyBox, 'network-address');
    const host = field(address, 'Proxy host', '192.168.1.10');
    const port = field(address, 'Port', '8080'); port.inputMode = 'numeric'; port.maxLength = 5;
    const loopback = element('p', LOOPBACK, proxyBox, 'runtime-feedback warning'); loopback.hidden = true;
    const bypassBox = element('div', undefined, proxyBox, 'network-bypass'); bypassBox.dataset.level = 'advanced';
    const bypass = field(bypassBox, 'No proxy for (comma separated)', 'localhost, *.example.com, 192.168.1.20');
    element('p', BYPASS_LATER, bypassBox, 'hint');
    element('p', 'The proxy is for HTTP and HTTPS traffic only. Some apps ignore it. SOCKS proxies, proxies that need a user name and password, and VPNs are not supported.', form, 'hint');

    element('h4', 'Private DNS', form);
    const modeLabel = element('label', undefined, form, 'network-field');
    element('span', 'Private DNS mode', modeLabel);
    const mode = element('select', undefined, modeLabel);
    for (const [value, label] of [['off', 'Off'], ['automatic', 'Automatic'], ['hostname', 'Private DNS provider hostname']])
      element('option', label, mode).value = value;
    mode.value = 'automatic';
    const dnsBox = element('div', undefined, form, 'network-dns');
    const dnsHost = field(dnsBox, 'Provider hostname', 'dns.adguard.com');

    const problem = element('p', '', form, 'runtime-feedback error'); problem.setAttribute('role', 'alert'); problem.hidden = true;
    const actions = element('div', undefined, form, 'runtime-actions');
    const applyButton = element('button', 'Apply', actions, 'btn btn-primary'); applyButton.type = 'button';
    const resetButton = element('button', 'Reset', actions, 'btn'); resetButton.type = 'button';
    resetButton.title = 'Discard changes and show what this device uses now';
    const status = element('p', '', panel); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');

    // `loaded` is the last answer of the device; `asked` stops a failed load from repeating on every render.
    let runtime = null, epoch = 0, busy = false, loading = false, loaded = null, asked = false;
    const current = (token, id) => token === epoch && runtime?.id === id;
    // A full sentence, so the advice that follows reads well.
    const message = error => { const text = String(error?.message || error).trim(); return /[.!?]$/.test(text) ? text : text + '.'; };

    function say(text, kind) {
      status.textContent = text;
      status.className = text && kind ? 'runtime-feedback ' + kind : '';
    }
    function complain(text) { problem.textContent = text; problem.hidden = !text; }
    function lock() {
      const running = runtime?.state === 'Running', ready = running && Boolean(loaded) && !busy && !loading;
      blocked.hidden = running;
      blocked.textContent = runtime?.state === 'Frozen' ? ASLEEP : STOPPED;
      for (const control of [useProxy, mode, applyButton]) control.disabled = !ready;
      for (const control of [host, port, bypass]) control.disabled = !ready || !useProxy.checked;
      dnsHost.disabled = !ready || mode.value !== 'hostname';
      proxyBox.hidden = !useProxy.checked;
      dnsBox.hidden = mode.value !== 'hostname';
      loopback.hidden = !useProxy.checked || !isLoopback(host.value);
      resetButton.disabled = !running || busy || loading;
      applyButton.textContent = busy ? 'Applying…' : 'Apply';
    }
    function fill(report) {
      useProxy.checked = Boolean(report?.proxy);
      host.value = report?.proxy?.host || '';
      port.value = report?.proxy ? String(report.proxy.port) : '';
      bypass.value = (report?.proxy?.bypass || []).join(', ');
      mode.value = report?.dns.mode || 'automatic';
      dnsHost.value = report?.dns.hostname || '';
      complain('');
    }
    function forget() { epoch++; busy = false; loading = false; loaded = null; asked = false; }

    async function load() {
      if (!runtime || runtime.state !== 'Running' || busy || loading) return;
      const token = epoch, id = runtime.id;
      loading = true; asked = true; lock(); say('Loading network settings…');
      try {
        const report = validate(await request({op:'network_get', id}), id);
        if (!current(token, id)) return;
        loaded = report; fill(report); say('');
      } catch (error) {
        if (current(token, id)) { loaded = null; say(`Could not read the network settings: ${message(error)} Use Reset to retry.`, 'error'); }
      } finally {
        if (current(token, id)) { loading = false; lock(); }
      }
    }
    async function apply() {
      if (!runtime || runtime.state !== 'Running' || busy || loading || !loaded) return;
      const checked = check({proxy:useProxy.checked, host:host.value, port:port.value, bypass:bypass.value, mode:mode.value, hostname:dnsHost.value});
      if (checked.problem) { complain(checked.problem); return; }
      const token = epoch, id = runtime.id;
      busy = true; complain(''); lock(); say('Applying network settings…');
      try {
        const report = validate(await request({op:'network_set', id, ...checked.fields}), id);
        if (!current(token, id)) return;
        // The form always shows what Android reports, also after a partial failure.
        loaded = report; fill(report);
        if (report.error) say(`${message(report.error)} The form shows what this device uses now.`, 'error');
        else say(`Network settings applied.${report.proxy?.bypass.length ? ' ' + BYPASS_LATER : ''}`, 'success');
      } catch (error) {
        if (current(token, id)) say(`Could not apply the network settings: ${message(error)}`, 'error');
      } finally {
        if (current(token, id)) { busy = false; lock(); }
      }
    }
    for (const control of [host, port, bypass, dnsHost]) control.addEventListener('input', () => { complain(''); lock(); });
    for (const control of [useProxy, mode]) control.addEventListener('change', () => { complain(''); lock(); });
    applyButton.addEventListener('click', apply);
    resetButton.addEventListener('click', () => { if (loaded) fill(loaded); return load(); });
    lock();
    return {
      mount(target, value) {
        if (panel.parentNode !== target) target.append(panel);
        const running = value.state === 'Running';
        // Another device, or a device that stopped: what was loaded may no longer be true.
        // What was typed stays visible while the device sleeps. It is read again when the device runs.
        if (runtime?.id !== value.id) { forget(); fill(null); say(''); }
        else if (!running && runtime.state === 'Running') { forget(); say(''); }
        runtime = {id:value.id, name:value.name || 'this device', state:value.state};
        panel.dataset.runtimeId = value.id;
        lock();
        // The page re-renders often. Read the settings once per opened section, not on every call.
        if (running && !loaded && !asked) return load();
      },
      deactivate() {
        forget(); runtime = null; fill(null); say(''); lock(); panel.remove();
      },
    };
  }
  const api = {create, check, validate, parseBypass, isLoopback};
  root.AnvilRuntimeNetwork = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
