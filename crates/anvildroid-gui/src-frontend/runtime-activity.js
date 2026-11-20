// Activity panel of Device settings: the busiest Android processes and the frame rate of the app in front.
// The controller answers with raw counters (services/runtime-performance.py). Rates come from two
// consecutive answers, so the first one shows memory only. Polls run only while the section is open,
// the window is visible and the device runs.
(function(root) {
  const INTERVAL = 2000, TOP = 12;
  const APP_UID = 10000, NOTABLE_KB = 50 * 1024, NOTABLE_CPU = 0.5;
  // First sentences of the controller's refusals (runtime-inspect.py). They are a state, not a failure.
  const ASLEEP = 'This device is asleep.', STOPPED = 'Start this device first.';
  const ASLEEP_HINT = 'This device is asleep. Open an app or its Android window to see activity.';
  const STOPPED_HINT = 'Start this device to see its activity.';
  const IDLE_FRAMES = 'No new frames since the last update. This is normal for a still screen.';
  const PACKAGE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$/;
  const whole = value => Number.isSafeInteger(value) && value >= 0;
  const positive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
  const text = (value, limit) => typeof value === 'string' && value.length <= limit;

  function validate(value, id) {
    const bad = () => { throw new Error('Invalid activity response or another device.'); };
    if (!value || typeof value !== 'object' || value.runtime_id !== id || typeof value.uptime !== 'number' || !Number.isFinite(value.uptime) ||
        value.uptime < 0 || !whole(value.ticks) || value.ticks < 1 || (value.cpus !== null && !(whole(value.cpus) && value.cpus > 0)) ||
        !value.unavailable || typeof value.unavailable !== 'object' || Array.isArray(value.unavailable) ||
        Object.values(value.unavailable).some(reason => !text(reason, 512))) bad();
    if (value.processes !== null) {
      if (!Array.isArray(value.processes) || value.processes.length > 1024) bad();
      for (const row of value.processes)
        if (!row || !whole(row.pid) || !whole(row.start) || !whole(row.ticks) || !whole(row.rss_kb) || (row.uid !== null && !whole(row.uid)) ||
            !text(row.name, 256) || !row.name) bad();
    }
    const focus = value.focus, frames = value.frames, surface = value.surface;
    if (focus !== null && (!focus || !text(focus.package, 255) || !PACKAGE.test(focus.package) || !text(focus.activity, 512))) bad();
    if (frames !== null && (!frames || !whole(frames.pid) || !whole(frames.total) || !text(frames.since, 32) || !whole(frames.uptime_ms))) bad();
    if (surface !== null && (!surface || !text(surface.layer, 512) || !['surfaceview', 'window'].includes(surface.kind) ||
        (surface.refresh_hz !== null && !positive(surface.refresh_hz)) || (surface.newest !== null && !text(surface.newest, 32)) ||
        (surface.fps !== null && !(typeof surface.fps === 'number' && Number.isFinite(surface.fps) && surface.fps >= 0)) ||
        (surface.newest === null) !== (surface.fps === null))) bad();
    return value;
  }

  function formatMemory(kib) {
    if (!whole(kib)) return '—';
    if (kib >= 1024 ** 2) return (kib / 1024 ** 2).toFixed(2) + ' GiB';
    return Math.max(1, Math.round(kib / 1024)) + ' MiB';
  }
  // "com.example.app:push" is the "push" process of com.example.app.
  function splitName(name) {
    const cut = name.indexOf(':');
    return cut > 0 && cut < name.length - 1 ? {main:name.slice(0, cut), part:name.slice(cut + 1)} : {main:name, part:''};
  }
  // The same base as the CPU bar of the device list: 100% means every CPU the device may use is busy.
  // Without a known count the figure is per core and may pass 100%.
  function capacityOf(record, report) {
    const quota = record?.resources?.effective?.cpu_quota_count, host = record?.resources?.host?.cpu_count;
    return positive(quota) ? quota : positive(host) ? host : positive(report?.cpus) ? report.cpus : null;
  }

  // CPU share of every process between two answers. A process is the same one only when
  // its PID and its start time match, so a reused PID never inherits old counters.
  function usage(previous, current, capacity) {
    const seconds = previous ? current.uptime - previous.uptime : 0;
    const measured = Boolean(previous?.processes && previous.ticks === current.ticks && seconds >= 0.2 && seconds <= 60);
    const before = new Map(measured ? previous.processes.map(row => [row.pid, row]) : []);
    const share = ticks => {
      const value = ticks / current.ticks / seconds / (capacity || 1) * 100;
      return capacity ? Math.min(100, value) : value;
    };
    const rows = (current.processes || []).map(row => {
      const old = before.get(row.pid);
      let cpu = null;
      if (measured && old && old.start === row.start) cpu = row.ticks >= old.ticks ? share(row.ticks - old.ticks) : null;
      // Started after the previous answer: all of its time was used in this interval.
      else if (measured && row.start / current.ticks >= previous.uptime) cpu = share(row.ticks);
      return {...row, cpu};
    });
    return {rows, measured};
  }

  // Apps and anything busy or large, busiest first. The rest becomes one summary.
  function pick(rows, limit = TOP) {
    const notable = row => row.uid === null || row.uid >= APP_UID || (row.cpu ?? 0) >= NOTABLE_CPU || row.rss_kb >= NOTABLE_KB;
    const rank = row => Math.round((row.cpu ?? 0) * 10);
    const sorted = [...rows].sort((a, b) => rank(b) - rank(a) || b.rss_kb - a.rss_kb || a.pid - b.pid);
    const shown = sorted.filter(notable).slice(0, limit), kept = new Set(shown);
    const others = sorted.filter(row => !kept.has(row));
    return {shown, rest:{count:others.length, rss_kb:others.reduce((sum, row) => sum + row.rss_kb, 0),
      cpu:others.some(row => row.cpu !== null) ? others.reduce((sum, row) => sum + (row.cpu ?? 0), 0) : null}};
  }

  // Frames per second of the app in front. Two optional sources: the UI toolkit counter
  // (frames drawn between two answers) and the newest frames of the app's surface.
  // A still screen is a real 0. An unknown rate is null with a reason.
  function frameRate(previous, current) {
    const focus = current.focus, refresh = current.surface?.refresh_hz ?? null;
    if (!focus) return {fps:null, refresh:null, measuring:false, reason:current.unavailable.focus || 'No app is in front.'};
    if (!current.frames && !current.surface)
      return {fps:null, refresh, measuring:false, reason:current.unavailable.frames || current.unavailable.surface || 'This app does not report its frames.'};
    const same = previous?.focus?.package === focus.package;
    const values = [];
    let measuring = false;
    if (current.frames) {
      const now = current.frames, old = same ? previous.frames : null;
      const seconds = old ? (now.uptime_ms - old.uptime_ms) / 1000 : 0;
      // A restarted process or reset statistics start a new count. A counter never runs backwards.
      if (old && old.pid === now.pid && old.since === now.since && now.total >= old.total && seconds >= 0.2 && seconds <= 60)
        values.push((now.total - old.total) / seconds);
      else measuring = true;
    }
    if (current.surface) {
      const now = current.surface, old = same ? previous.surface : null;
      if (now.newest === null) values.push(0);
      else if (!old) measuring = true;
      else values.push(old.newest === now.newest ? 0 : now.fps);
    }
    const fps = values.length ? Math.max(...values) : null;
    if (fps === null || (measuring && fps === 0) || fps > 1000) return {fps:null, refresh, measuring:true, reason:''};
    return {fps, refresh, measuring:false, reason:fps < 0.5 ? IDLE_FRAMES : ''};
  }

  function create({request, document, timers = {set:(task, delay) => setTimeout(task, delay), clear:id => clearTimeout(id)}}) {
    const make = (tag, className = '', content = '') => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (content) node.textContent = content;
      return node;
    };
    const panel = make('section', 'runtime-inline-feature runtime-activity');
    const notice = make('p', 'hint activity-notice');
    notice.setAttribute('role', 'status');
    const retry = make('button', 'btn', 'Retry');
    retry.type = 'button';
    const content = make('div', 'activity-content');
    const frontName = make('span', 'activity-front-name'), frontRate = make('span', 'activity-front-rate'), frontHint = make('p', 'hint activity-front-hint');
    const front = make('div', 'activity-front');
    const cell = (label, value) => { const box = make('div', 'activity-front-cell'); box.append(make('span', 'activity-label', label), value); return box; };
    front.append(cell('App in front', frontName), cell('Frame rate', frontRate));
    const table = make('table', 'activity-table'), head = make('thead'), headRow = make('tr'), body = make('tbody');
    const memoryHead = make('th', 'activity-number', 'Memory');
    memoryHead.title = 'Memory this process holds in RAM. Memory shared by several processes is counted in each of them.';
    headRow.append(make('th', '', 'Process'), make('th', 'activity-number', 'CPU'), memoryHead);
    for (const item of headRow.children || []) item.setAttribute('scope', 'col');
    head.append(headRow);
    table.append(head, body);
    const tableHint = make('p', 'hint activity-table-hint'), rest = make('p', 'hint activity-rest'), note = make('p', 'hint activity-note');
    content.append(front, frontHint, table, tableHint, rest, note);
    panel.append(notice, retry, content);

    let record = null, mounted = false, timer = null, busy = false, epoch = 0, previous = null, halted = false;
    const visible = () => document.visibilityState !== 'hidden';
    const wanted = () => mounted && record?.state === 'Running' && !halted && visible();
    function stop() {
      if (timer !== null) timers.clear(timer);
      timer = null;
    }
    // Forget the last answer and disown a request that is still on its way.
    function reset() { previous = null; epoch++; }
    function say(message, kind = 'hint', withRetry = false) {
      notice.className = kind === 'error' ? 'runtime-feedback error activity-notice' : 'hint activity-notice';
      notice.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      notice.textContent = message;
      notice.hidden = !message;
      retry.hidden = !withRetry;
      content.hidden = Boolean(message);
    }
    function schedule(delay) {
      if (timer !== null || !wanted()) return;
      timer = timers.set(() => { timer = null; poll(); }, delay);
    }
    function kick() {
      if (!busy && timer === null && wanted()) poll();
    }
    async function poll() {
      // One request at a time: the next one is only planned when this one has ended.
      if (busy || !wanted()) return;
      busy = true;
      const mine = epoch, id = record.id;
      try {
        const report = validate(await request({op:'performance_get', id}), id);
        if (mine !== epoch) return;
        show(report);
        previous = report;
      } catch (error) {
        if (mine !== epoch) return;
        halted = true;
        previous = null;
        const message = String(error?.message || error).trim().replace(/([^.!?…])$/, '$1.');
        if (message.includes(ASLEEP)) say(ASLEEP_HINT, 'hint', true);
        else if (message.includes(STOPPED)) say(STOPPED_HINT, 'hint', true);
        else say(`Could not read the activity of this device. ${message} Use Retry.`, 'error', true);
      } finally {
        busy = false;
        schedule(mine === epoch ? INTERVAL : 0);
      }
    }
    function show(report) {
      const capacity = capacityOf(record, report);
      const rate = frameRate(previous, report);
      frontName.textContent = report.focus ? report.focus.package : '—';
      frontName.title = report.focus ? `${report.focus.package}/${report.focus.activity}` : '';
      frontRate.textContent = rate.measuring ? 'measuring…'
        : (rate.fps === null ? '—' : Math.round(rate.fps) + ' FPS') + (rate.refresh && report.focus ? ` · display ${Math.round(rate.refresh)} Hz` : '');
      frontHint.textContent = rate.reason;
      frontHint.hidden = !rate.reason;

      const {rows, measured} = usage(previous, report, capacity);
      table.hidden = !report.processes;
      tableHint.hidden = Boolean(report.processes);
      tableHint.textContent = report.processes ? '' : report.unavailable.processes || 'Android did not list its processes.';
      const {shown, rest:others} = pick(rows);
      body.replaceChildren(...shown.map(row => {
        const line = make('tr'), nameCell = make('td'), name = make('div', 'activity-name'), parts = splitName(row.name);
        nameCell.title = `${row.name} (PID ${row.pid})`;
        nameCell.append(name);
        name.append(make('span', 'activity-name-main', parts.main));
        if (parts.part) name.append(make('span', 'activity-name-part', parts.part));
        if (report.focus && parts.main === report.focus.package) name.append(make('span', 'activity-tag', 'In front'));
        line.append(nameCell, make('td', 'activity-number', row.cpu === null ? (measured ? '—' : 'measuring…') : row.cpu.toFixed(1) + '%'),
          make('td', 'activity-number', formatMemory(row.rss_kb)));
        return line;
      }));
      rest.hidden = !report.processes || !others.count;
      rest.textContent = others.count ? `${others.count} more ${others.count === 1 ? 'process uses' : 'processes use'} ` +
        `${others.cpu === null ? '' : others.cpu.toFixed(1) + '% CPU and '}${formatMemory(others.rss_kb)}.` : '';
      note.textContent = 'Updates every 2 seconds while this section is open. ' +
        (capacity ? `CPU 100% means all ${capacity} CPUs of this device are busy. ` : 'CPU 100% means one CPU core is busy. ') +
        'Measuring does not keep this device awake.';
      say('');
    }
    retry.addEventListener('click', () => {
      if (!mounted || !record) return;
      halted = false;
      if (record.state === 'Running') say('Reading activity…');
      kick();
    });
    if (typeof document.addEventListener === 'function') document.addEventListener('visibilitychange', () => {
      if (!visible()) { stop(); reset(); }
      else kick();
    });

    return {
      mount(host, next) {
        if (panel.parentNode !== host) host.append(panel);
        const changed = !mounted || !record || record.id !== next.id, moved = changed || record.state !== next.state;
        record = next;
        mounted = true;
        if (changed || (moved && next.state !== 'Running')) reset();
        // A poll planned for the old device or state must not delay the first one of the new.
        if (moved) { halted = false; stop(); }
        if (next.state !== 'Running') {
          stop();
          say(next.state === 'Frozen' ? ASLEEP_HINT : STOPPED_HINT);
          return;
        }
        if (moved) say('Reading activity…');
        kick();
      },
      deactivate() {
        mounted = false;
        stop();
        reset();
        record = null;
        halted = false;
        panel.remove();
      },
    };
  }

  const api = {create, validate, usage, pick, frameRate, formatMemory, splitName, capacityOf, INTERVAL};
  root.AnvilRuntimeActivity = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
