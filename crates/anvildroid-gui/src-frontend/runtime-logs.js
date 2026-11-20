// Android log lines of one running device, embedded in device settings.
// The controller reads, bounds and redacts the lines; this file follows, filters, copies and saves them.
(function(root) {
  const LEVELS = [['V', 'Verbose'], ['D', 'Debug'], ['I', 'Info'], ['W', 'Warning'], ['E', 'Error'], ['F', 'Fatal']];
  const PACKAGE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$/;
  const POLL_MS = 2000;          // while following
  const REQUEST_LINES = 1000;    // per answer; the controller accepts 50 to 2000
  const MAX_KEPT = 4000;         // lines kept in the view; the oldest are dropped
  const MAX_SAVE = 8 * 1024 * 1024;
  const GAP = '… some lines were skipped because too many arrived at once …';
  // The controller's own words for a device that cannot answer now: not an error.
  const CALM = /This device is asleep|Start this device first/;
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const reason = error => String(error?.message || error);

  function validate(value, id) {
    const bad = () => { throw new Error('Invalid log answer or another device.'); };
    if (!value || value.runtime_id !== id || !Array.isArray(value.lines) || value.lines.length > 2000 || typeof value.truncated !== 'boolean' ||
        (value.cursor !== null && typeof value.cursor !== 'string') || (value.note != null && typeof value.note !== 'string')) bad();
    for (const item of value.lines)
      if (!item || !count(item.time) || !count(item.pid) || !count(item.tid) || !LEVELS.some(([level]) => level === item.level) ||
          typeof item.tag !== 'string' || typeof item.message !== 'string') bad();
    return value;
  }
  // "11-19 15:31:29.892  7099  7138 D BootAnimation: text", in local time like `logcat -v threadtime`.
  function formatLine(item) {
    const date = new Date(item.time), two = value => String(value).padStart(2, '0');
    const time = `${two(date.getMonth() + 1)}-${two(date.getDate())} ${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}.${String(date.getMilliseconds()).padStart(3, '0')}`;
    return `${time} ${String(item.pid).padStart(5)} ${String(item.tid).padStart(5)} ${item.level} ${item.tag}: ${item.message}`;
  }
  function fileName(name, date = new Date()) {
    const two = value => String(value).padStart(2, '0');
    const safe = String(name || '').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'device';
    return `android-logs-${safe}-${date.getFullYear()}${two(date.getMonth() + 1)}${two(date.getDate())}-${two(date.getHours())}${two(date.getMinutes())}${two(date.getSeconds())}.txt`;
  }

  function create({request, invoke, document, toast = () => {}, dialog, clipboard = root.navigator?.clipboard,
                   later = (callback, ms) => setTimeout(callback, ms), cancel = handle => clearTimeout(handle)}) {
    function element(tag, text, parent, className) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      if (parent) parent.append(node);
      return node;
    }
    function field(label, control, parent) {
      const wrap = element('label', undefined, parent, 'logs-field');
      element('span', label, wrap);
      wrap.append(control);
      return control;
    }
    function button(label, parent, className = 'btn') {
      const node = element('button', label, parent, className);
      node.type = 'button';
      return node;
    }
    const panel = element('section', undefined, null, 'runtime-inline-feature runtime-logs');
    element('p', 'Recent Android log lines from this device. Logs may contain personal data, so read them before you share them. Passwords and keys that are recognised are hidden.', panel, 'hint');
    const controls = element('div', undefined, panel, 'logs-controls');
    const levelSelect = field('Lowest level', element('select'), controls);
    for (const [value, label] of LEVELS) element('option', label, levelSelect).value = value;
    const packageInput = field('Only this app (package name)', element('input'), controls);
    packageInput.type = 'text'; packageInput.placeholder = 'com.example.app'; packageInput.spellcheck = false; packageInput.maxLength = 255;
    const filterInput = field('Filter shown lines', element('input'), controls);
    filterInput.type = 'search'; filterInput.placeholder = 'Text, tag or process ID'; filterInput.spellcheck = false;
    const actions = element('div', undefined, panel, 'runtime-actions');
    const followButton = button('Pause', actions), clearButton = button('Clear', actions),
      copyButton = button('Copy', actions), saveButton = button('Save…', actions);
    const status = element('p', '', panel, 'hint logs-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const problem = element('p', '', panel, 'runtime-feedback'); problem.hidden = true;
    const retryButton = button('Retry', panel); retryButton.hidden = true;
    const view = element('div', undefined, panel, 'logs-view');
    view.setAttribute('role', 'log'); view.setAttribute('aria-label', 'Android log lines'); view.tabIndex = 0;

    let runtime = null, epoch = 0, entries = [], cursor = null, loaded = false, following = true, inflight = false,
      timer = null, failure = null, note = '', level = 'I', appPackage = null, needle = '';
    levelSelect.value = level;
    const running = () => runtime?.state === 'Running';
    const matches = entry => !needle || entry.text.toLowerCase().includes(needle);
    const shown = () => entries.filter(entry => !entry.node.hidden);

    function stopTimer() {
      if (timer !== null) { cancel(timer); timer = null; }
    }
    function forget() {
      epoch++; stopTimer();
      entries = []; cursor = null; loaded = false; failure = null; note = '';
      view.replaceChildren();
    }
    function show() {
      const visible = shown().length;
      let text = '';
      if (runtime?.state === 'Frozen') text = 'This device is asleep. Open an app or the Android window to see new log lines.';
      else if (runtime && !running()) text = 'Start this device to see its logs.';
      else if (failure) text = '';
      else if (!loaded) text = 'Loading logs…';
      else if (note) text = note;
      else if (!entries.length) text = 'No log lines at this level yet.';
      const lines = !entries.length ? '' : visible === entries.length ? `${visible} lines.` : `${visible} of ${entries.length} lines shown.`;
      const paused = running() && !failure && loaded && !following ? 'Paused.' : '';
      status.textContent = [text, lines, paused].filter(Boolean).join(' ');
      problem.textContent = failure ? failure.text : '';
      problem.className = failure && !failure.calm ? 'runtime-feedback error' : 'runtime-feedback';
      if (failure && !failure.calm) problem.setAttribute('role', 'alert'); else problem.setAttribute('role', 'status');
      problem.hidden = !failure;
      retryButton.hidden = !failure || !running();
      followButton.textContent = following ? 'Pause' : 'Follow';
      followButton.setAttribute('aria-pressed', String(following));
      followButton.title = following ? 'Stop reading new lines' : 'Keep reading new lines';
      clearButton.disabled = !entries.length;
      copyButton.disabled = saveButton.disabled = !visible;
    }
    function add(items, gap) {
      // Follow the end only while the user is already there; never pull them away from older lines.
      const atEnd = view.scrollHeight - view.scrollTop - view.clientHeight <= 24;
      const append = (text, levelName) => {
        const node = element('div', text, view, levelName ? 'log-line' : 'log-line log-gap');
        if (levelName) node.dataset.level = levelName;
        const entry = {text, node};
        node.hidden = !matches(entry);
        entries.push(entry);
      };
      if (gap) append(GAP, '');
      for (const item of items) append(formatLine(item), item.level);
      if (entries.length > MAX_KEPT) for (const old of entries.splice(0, entries.length - MAX_KEPT)) old.node.remove();
      if (atEnd && items.length) view.scrollTop = view.scrollHeight;
    }
    // One request at a time, also across closing and reopening the section.
    async function read() {
      if (inflight || !running()) return;
      stopTimer();
      inflight = true;
      const token = epoch, id = runtime.id, since = cursor;
      show();
      try {
        const report = validate(await request({op:'logs_get', id, lines:REQUEST_LINES, level, package:appPackage, since}), id);
        if (token === epoch) {
          add(report.lines, since !== null && report.truncated);
          cursor = report.cursor; note = report.note || ''; loaded = true;
        }
      } catch (error) {
        if (token === epoch) {
          const text = reason(error), calm = CALM.test(text);
          failure = {calm, text:calm ? text : `Could not read the logs: ${text} Use Retry to try again.`};
        }
      } finally {
        inflight = false;
        sync();
      }
    }
    // Decide what should happen next. Safe to call at any time and any number of times.
    function sync() {
      if (!runtime) return;
      if (!running() || failure) stopTimer();
      else if (!inflight) {
        if (!loaded) return void read();
        if (following && timer === null) timer = later(() => { timer = null; read(); }, POLL_MS);
      }
      show();
    }
    function restart() {
      forget();
      sync();
    }

    levelSelect.addEventListener('change', () => {
      if (!LEVELS.some(([value]) => value === levelSelect.value) || levelSelect.value === level) return;
      level = levelSelect.value;
      restart();
    });
    packageInput.addEventListener('change', () => {
      const value = String(packageInput.value || '').trim();
      if (value && (value.length > 255 || !PACKAGE.test(value))) {
        packageInput.setAttribute('aria-invalid', 'true');
        toast('Enter a full app package name, for example com.example.app.', 'error');
        return;
      }
      packageInput.setAttribute('aria-invalid', 'false');
      if ((value || null) === appPackage) return;
      appPackage = value || null;
      restart();
    });
    filterInput.addEventListener('input', () => {
      needle = String(filterInput.value || '').trim().toLowerCase();
      for (const entry of entries) entry.node.hidden = !matches(entry);
      show();
    });
    followButton.addEventListener('click', () => {
      following = !following;
      if (!following) { stopTimer(); show(); }
      else if (running() && !failure && !inflight) read();
      else sync();
    });
    retryButton.addEventListener('click', () => {
      failure = null;
      if (running() && !inflight) read(); else sync();
    });
    clearButton.addEventListener('click', () => {
      // The position is kept: cleared lines do not come back.
      entries = []; view.replaceChildren(); show();
    });
    copyButton.addEventListener('click', async () => {
      const lines = shown();
      if (!lines.length) return;
      try {
        if (typeof clipboard?.writeText !== 'function') throw new Error('No clipboard');
        await clipboard.writeText(lines.map(entry => entry.text).join('\n') + '\n');
        toast(`${lines.length} log lines copied. They may contain personal data.`, 'success');
      } catch (_) {
        toast('Copying was not allowed. Use Save… to keep these lines in a file.', 'error');
      }
    });
    saveButton.addEventListener('click', async () => {
      const lines = shown();
      if (!lines.length || !runtime) return;
      const content = lines.map(entry => entry.text).join('\n') + '\n';
      if (content.length > MAX_SAVE) return toast('These logs are too large to save. Filter them or clear the view, then retry.', 'error');
      try {
        const path = await dialog.save({defaultPath:fileName(runtime.name), title:'Save logs', filters:[{name:'Text file', extensions:['txt']}]});
        if (!path) return;
        await invoke('save_device_log', {path:String(path), content});
        toast(`${lines.length} log lines saved to ${path}. They may contain personal data.`, 'success');
      } catch (error) {
        toast(`Could not save the logs: ${reason(error)}`, 'error');
      }
    });
    show();
    return {
      mount(host, value) {
        if (panel.parentNode !== host) host.append(panel);
        if (runtime?.id !== value.id) forget();
        // A new state is a new chance: an error from the old state says nothing about this one.
        else if (runtime.state !== value.state) failure = null;
        runtime = {id:value.id, name:value.name || 'this device', state:value.state};
        panel.dataset.runtimeId = value.id;
        sync();
      },
      deactivate() {
        forget();
        runtime = null;
        panel.remove();
      },
    };
  }
  const api = {create, validate, formatLine, fileName, POLL_MS, MAX_KEPT, REQUEST_LINES};
  root.AnvilRuntimeLogs = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
