// Backups of one device's Android data, embedded in device settings.
// The controller owns every path and check; this file lists, asks and shows progress.
(function(root) {
  // First sentence of the controller's refusal; restoring anyway needs a second, explicit yes.
  const IMAGE_MISMATCH = 'This backup was made with a different Android image.';
  const STATUSES = ['Running', 'Succeeded', 'Failed', 'Cancelled'];
  const size = value => Number.isSafeInteger(value) && value >= 0;

  function formatBytes(bytes) {
    if (!size(bytes)) return 'unknown size';
    if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + ' GiB';
    if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(1) + ' MiB';
    return Math.max(1, Math.round(bytes / 1024)) + ' KiB';
  }
  function formatDate(seconds) {
    return new Date(seconds * 1000).toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
  }
  function isImageMismatch(error) {
    return String(error?.message || error).includes(IMAGE_MISMATCH);
  }
  function validate(value, id) {
    const bad = () => { throw new Error('Invalid backup response or another device; refresh before retrying.'); };
    if (!value || value.runtime_id !== id || !Array.isArray(value.backups) || value.backups.length > 64 ||
        typeof value.ready !== 'boolean' || typeof value.has_data !== 'boolean' || (value.blocked !== null && typeof value.blocked !== 'string') ||
        (value.limit != null && typeof value.limit !== 'string') || (value.notice != null && typeof value.notice !== 'string') ||
        !value.location || typeof value.location.path !== 'string' || typeof value.location.external !== 'boolean') bad();
    const seen = new Set();
    for (const item of value.backups) {
      if (!item || !/^[0-9a-f]{32}$/.test(item.id) || seen.has(item.id) || !size(item.created) || !size(item.archive_bytes) ||
          !size(item.data_bytes) || typeof item.available !== 'boolean' || ![true, false, null].includes(item.image_matches) ||
          (item.sdk !== null && !Number.isSafeInteger(item.sdk)) || typeof item.image_label !== 'string') bad();
      seen.add(item.id);
    }
    const job = value.job;
    if (job !== null && (!job || !/^[0-9a-f]{32}$/.test(job.id) || !['backup', 'restore'].includes(job.action) ||
        !STATUSES.includes(job.status) || typeof job.phase !== 'string' || typeof job.cancellable !== 'boolean' ||
        (job.error !== null && typeof job.error !== 'string') || (job.warning != null && typeof job.warning !== 'string'))) bad();
    return value;
  }
  function describe(item) {
    return [formatBytes(item.archive_bytes), item.image_label, item.sdk ? 'Android SDK ' + item.sdk : ''].filter(Boolean).join(' · ');
  }
  function progressOf(job, name) {
    const known = size(job.total_bytes) && job.total_bytes > 0 && size(job.done_bytes);
    return {
      title: `${job.action === 'backup' ? 'Backing up' : 'Restoring'} ${name}`,
      detail: `${job.phase}${known ? ` ${formatBytes(Math.min(job.done_bytes, job.total_bytes))} of ${formatBytes(job.total_bytes)}.` : ''} Keep AnvilDroid open.`,
      percent: known ? Math.min(100, job.done_bytes / job.total_bytes * 100) : null,
    };
  }
  function restoreMessage(item, name) {
    const risk = item.image_matches === false
      ? '\n\nWarning: this backup was made with a different Android image than the one this device uses now. Apps and settings from it may not work, and Android may fail to start.'
      : '';
    return `Restore ${name} to the backup from ${formatDate(item.created)}?\n\nEverything on this device now — apps, app data and Android settings — is replaced by the backup. This cannot be undone. The current data is kept until the restore has succeeded.${risk}`;
  }

  // Start a backup or restore and follow it to the end. Resolves with the last
  // report; rejects with the controller's reason (error.cancelled when the user cancelled).
  async function run({request, id, name, start, showProgress = () => {}, clearProgress = () => {}, update = () => {},
                      sleep = ms => new Promise(resolve => setTimeout(resolve, ms))}) {
    const key = 'backup-' + id;
    let report = validate(await request(start), id);
    const jobId = report.job?.id;
    if (!jobId || report.job.action !== (start.op === 'backup_create' ? 'backup' : 'restore'))
      throw new Error('The controller did not start the operation; refresh and retry.');
    let failures = 0;
    try {
      for (;;) {
        const job = report.job;
        if (job?.id !== jobId) throw new Error('Another operation replaced this one; check the backup list.');
        update(report);
        if (job.status === 'Succeeded') return report;
        if (job.status === 'Cancelled') throw Object.assign(new Error('Cancelled.'), {cancelled:true});
        if (job.status === 'Failed') throw new Error(job.error || 'The operation failed.');
        showProgress(key, {...progressOf(job, name),
          cancel: job.cancellable ? () => { Promise.resolve(request({op:'backup_cancel', id})).catch(() => {}); } : undefined});
        await sleep(1000);
        try { report = validate(await request({op:'backups_get', id}), id); failures = 0; }
        // The job keeps running in the controller; a few missed polls are not a failure.
        catch (error) { if (++failures >= 5) throw new Error(`Lost contact with the operation: ${String(error.message || error)} Check the backup list before retrying.`); }
      }
    } finally { clearProgress(key); }
  }

  // Reinstall erases Android data: offer a backup first. Resolves true (back up
  // first), false (reinstall without) and never offers when backups are unavailable.
  async function offerBeforeReinstall({request, confirm, id, name}) {
    let report;
    try { report = validate(await request({op:'backups_get', id}), id); }
    catch (_) { return false; }
    // Nothing to keep on a device that never ran; a full list cannot take another backup.
    if (!report.has_data || report.job?.status === 'Running' || report.limit) return false;
    return Boolean(await confirm(
      `Reinstall erases all apps, app data and Android settings on ${name}. A backup lets you bring them back later.\n\nBackups are stored in ${report.location.path}.`,
      {title:'Back up Android data first?', okLabel:'Back up first', cancelLabel:'Skip backup', kind:'info'}));
  }

  function create({request, document, toast = () => {}, confirm, showProgress = () => {}, clearProgress = () => {}, sleep,
                   later = (callback, ms) => setTimeout(callback, ms)}) {
    function element(tag, text, parent, className) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      if (parent) parent.append(node);
      return node;
    }
    const panel = element('section', undefined, null, 'runtime-inline-feature runtime-backups');
    element('p', 'A backup keeps this device’s apps, app data and Android settings, so you can bring them back after a reinstall. Stop the device before backing up or restoring.', panel, 'hint');
    const place = element('p', '', panel, 'hint');
    const list = element('ul', undefined, panel, 'backup-list');
    const notice = element('p', '', panel); notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
    const actions = element('div', undefined, panel, 'runtime-actions');
    const backUp = element('button', 'Back up now', actions, 'btn btn-primary'); backUp.type = 'button';
    let runtime = null, epoch = 0, busy = false, last = null, rowButtons = [];
    const current = (token, id) => token === epoch && runtime?.id === id;

    function lock() {
      backUp.disabled = busy || !last || !last.ready || !last.has_data || Boolean(last.limit);
      for (const {button, item, kind} of rowButtons)
        button.disabled = busy || !last || (kind === 'restore' ? !last.ready || !item.available : last.job?.status === 'Running');
    }
    function render(report) {
      last = report; rowButtons = []; list.replaceChildren();
      place.textContent = `Backups are stored in ${report.location.path}${report.location.external ? ' (your data folder, in a hidden folder that needs administrator access)' : '. Choose a data folder in Settings > Storage to keep them on another disk'}.`;
      for (const item of report.backups) {
        const row = element('li', undefined, list, 'backup-row');
        const text = element('div', undefined, row, 'backup-row-text');
        element('strong', formatDate(item.created), text);
        element('span', describe(item), text, 'hint');
        if (!item.available) element('span', 'File unavailable — connect the disk that holds your data folder', text, 'runtime-status warning');
        else if (item.image_matches === false) element('span', 'Made with a different Android image', text, 'runtime-status warning');
        const buttons = element('div', undefined, row, 'runtime-actions');
        const restore = element('button', 'Restore…', buttons, 'btn'); restore.type = 'button';
        restore.setAttribute('aria-label', `Restore backup from ${formatDate(item.created)}`);
        const remove = element('button', 'Delete…', buttons, 'btn btn-danger'); remove.type = 'button';
        remove.setAttribute('aria-label', `Delete backup from ${formatDate(item.created)}`);
        restore.addEventListener('click', () => restoreBackup(item));
        remove.addEventListener('click', () => deleteBackup(item));
        rowButtons.push({button:restore, item, kind:'restore'}, {button:remove, item, kind:'delete'});
      }
      if (!report.backups.length) {
        const empty = element('li', undefined, list, 'empty-state');
        element('p', 'No backups yet.', empty);
        element('p', 'Back up now before you reinstall this device or try something risky.', empty, 'hint');
      }
      if (!busy) notice.textContent = report.job?.status === 'Running' ? `${report.job.action === 'backup' ? 'A backup' : 'A restore'} is in progress…`
        : report.limit || report.blocked || (report.has_data ? '' : 'This device has no Android data yet. Start it once, then back up.');
      lock();
    }
    async function load() {
      if (!runtime) return;
      const token = epoch, id = runtime.id;
      try {
        const report = validate(await request({op:'backups_get', id}), id);
        if (!current(token, id)) return;
        render(report);
        // A job started elsewhere (reinstall, another window): keep the list current until it ends.
        if (report.job?.status === 'Running' && !busy) later(() => { if (current(token, id) && !busy) load(); }, 2000);
      } catch (error) {
        if (current(token, id)) { last = null; list.replaceChildren(); place.textContent = ''; notice.textContent = `${String(error.message || error)} Reopen this section to retry.`; lock(); }
      }
    }
    // One job at a time per panel; the progress toast stays visible on every page.
    async function follow(start, success) {
      const token = epoch, id = runtime.id, name = runtime.name;
      busy = true; lock(); notice.textContent = '';
      try {
        const report = await run({request, id, name, start, showProgress, clearProgress, sleep,
          update: value => { if (current(token, id)) { last = value; lock(); } }});
        // Restored, but the clean-up afterwards did not finish: say both, not "failed".
        if (report.job.warning) toast(report.job.warning, 'info'); else toast(success, 'success');
        if (current(token, id)) { render(report); if (report.job.warning) notice.textContent = report.job.warning; }
        return true;
      } catch (error) {
        if (error.cancelled) toast(`${start.op === 'backup_create' ? 'Backup' : 'Restore'} cancelled. Nothing was changed.`, 'info');
        else if (start.op === 'backup_restore' && !start.allow_image_mismatch && isImageMismatch(error)) throw error;
        else {
          const text = `${start.op === 'backup_create' ? 'Backup' : 'Restore'} failed: ${String(error.message || error)}`;
          toast(text, 'error');
          if (current(token, id)) notice.textContent = text;
        }
        return false;
      } finally {
        if (current(token, id)) busy = false;
        // The section may have been closed and reopened meanwhile: refresh what is shown now.
        if (runtime?.id === id && !busy) { lock(); load(); }
      }
    }
    async function restoreBackup(item) {
      if (!runtime || busy || !last?.ready) return;
      const token = epoch, id = runtime.id, name = runtime.name;
      if (!await confirm(restoreMessage(item, name), {title:'Restore backup', okLabel:'Replace and restore', kind:'warning'})) return;
      if (!current(token, id) || busy) return;
      const start = {op:'backup_restore', id, backup_id:item.id, allow_image_mismatch:item.image_matches === false};
      try { await follow(start, `${name} was restored from the backup. Start the device to use it.`); }
      catch (error) {
        // The controller compared the images itself and refused: ask again, plainly.
        if (!await confirm(`${String(error.message || error)}\n\nRestore this backup anyway?`,
          {title:'Different Android image', okLabel:'Restore anyway', kind:'warning'})) return;
        if (!current(token, id) || busy) return;
        await follow({...start, allow_image_mismatch:true}, `${name} was restored from the backup. Start the device to use it.`).catch(() => {});
      }
    }
    async function deleteBackup(item) {
      if (!runtime || busy) return;
      const token = epoch, id = runtime.id;
      if (!await confirm(`Delete the backup from ${formatDate(item.created)} (${formatBytes(item.archive_bytes)})? This cannot be undone. The device itself is not changed.`,
        {title:'Delete backup', okLabel:'Delete backup', kind:'warning'})) return;
      if (!current(token, id) || busy) return;
      busy = true; lock();
      try {
        const report = validate(await request({op:'backup_delete', id, backup_id:item.id}), id);
        if (report.notice) toast(report.notice, 'info'); else toast('Backup deleted.', 'success');
        if (current(token, id)) { busy = false; render(report); }
      } catch (error) {
        const text = `Could not delete the backup: ${String(error.message || error)}`;
        toast(text, 'error');
        if (current(token, id)) notice.textContent = text;
      } finally { busy = false; if (current(token, id)) lock(); }
    }
    backUp.addEventListener('click', () => {
      if (!runtime || busy || !last?.ready || !last.has_data || last.limit) return;
      follow({op:'backup_create', id:runtime.id}, `Backup of ${runtime.name} finished.`).catch(() => {});
    });
    lock();
    return {
      mount(host, value) {
        if (panel.parentNode !== host) host.append(panel);
        const same = runtime?.id === value.id && runtime.state === value.state;
        if (runtime?.id !== value.id) { epoch++; busy = false; last = null; list.replaceChildren(); place.textContent = ''; notice.textContent = 'Loading backups…'; }
        runtime = {id:value.id, name:value.name || 'this device', state:value.state};
        panel.dataset.runtimeId = value.id;
        lock();
        // The device card re-renders often; only a new device or state needs a new list.
        if (!same && !busy) return load();
      },
      deactivate() {
        // A running job keeps its progress toast; only this view goes away.
        if (runtime) epoch++;
        runtime = null; last = null; busy = false; list.replaceChildren(); panel.remove();
      },
      refresh: () => load(),
    };
  }
  const api = {create, run, offerBeforeReinstall, validate, progressOf, formatBytes, isImageMismatch, restoreMessage};
  root.AnvilRuntimeBackups = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
