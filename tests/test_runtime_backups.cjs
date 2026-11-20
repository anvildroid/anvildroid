const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Backups = require('../crates/anvildroid-gui/src-frontend/runtime-backups.js');

const ID = 'r-' + '1'.repeat(32), OTHER = 'r-' + '2'.repeat(32);
const A = 'a'.repeat(32), B = 'b'.repeat(32), JOB = 'c'.repeat(32);
const MISMATCH = 'This backup was made with a different Android image. Apps and settings from it may not work.';
const row = (id, extra = {}) => ({id, created:1794400000, archive_bytes:3 * 1024 ** 3, data_bytes:5 * 1024 ** 3, sdk:33,
  image_label:'Waydroid GAPPS', device_name:'Phone', image_matches:true, available:true, ...extra});
const report = (extra = {}) => ({runtime_id:ID, backups:[row(A), row(B, {image_matches:false, created:1793100000})], job:null, ready:true,
  blocked:null, limit:null, has_data:true, location:{path:'/mnt/data', external:true, available:true, error:null, free_bytes:1}, ...extra});
const job = (status, extra = {}) => ({id:JOB, action:'backup', backup_id:A, status, phase:'Backing up Android data…',
  done_bytes:1024 ** 3, total_bytes:4 * 1024 ** 3, cancellable:true, error:null, ...extra});
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };

function dom() {
  function node(tag) {
    return {tag, children:[], textContent:'', className:'', dataset:{}, attributes:{}, listeners:{}, disabled:false, parentNode:null,
      append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } },
      replaceChildren(...items) { this.children = []; this.append(...items); },
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
function panel(handler, answers = []) {
  const sent = [], dialogs = [], toasts = [], progress = [];
  const document = dom(), host = document.createElement('div');
  const tool = Backups.create({document, sleep:async () => {}, later:() => {},
    request:async request => { sent.push(request); return handler(request, sent); },
    toast:(message, kind) => toasts.push([kind, message]),
    confirm:async (message, options) => { dialogs.push({message, options}); return answers.length ? answers.shift() : true; },
    showProgress:(key, item) => progress.push([key, item]), clearProgress:key => progress.push([key, null])});
  const buttons = label => host.all(item => item.tag === 'button' && item.textContent === label);
  return {tool, host, sent, dialogs, toasts, progress, buttons};
}

test('reports are validated before anything is shown', () => {
  assert.equal(Backups.validate(report(), ID).backups.length, 2);
  for (const bad of [null, {}, report({runtime_id:OTHER}), report({backups:'none'}), report({ready:'yes'}), report({has_data:undefined}), report({location:null}),
      report({backups:[row('../etc')]}), report({backups:[row(A), row(A)]}), report({backups:[row(A, {archive_bytes:-1})]}),
      report({backups:[row(A, {image_matches:'no'})]}), report({backups:[row(A, {available:1})]}),
      report({job:job('Paused')}), report({job:job('Running', {action:'format'})}), report({job:job('Failed', {error:{}})})])
    assert.throws(() => Backups.validate(bad, ID), /Invalid backup response/);
});

test('sizes, progress and the mismatch sentence are presented plainly', () => {
  assert.equal(Backups.formatBytes(3650722201), '3.40 GiB');
  assert.equal(Backups.formatBytes(912 * 1024 ** 2), '912.0 MiB');
  assert.equal(Backups.formatBytes(10), '1 KiB');
  assert.equal(Backups.formatBytes(undefined), 'unknown size');
  const shown = Backups.progressOf(job('Running'), 'Phone');
  assert.deepEqual([shown.title, shown.percent], ['Backing up Phone', 25]);
  assert.match(shown.detail, /1\.00 GiB of 4\.00 GiB\. Keep AnvilDroid open\./);
  const unknown = Backups.progressOf(job('Running', {action:'restore', total_bytes:null, phase:'Checking the restored data…'}), 'Phone');
  assert.deepEqual([unknown.title, unknown.percent], ['Restoring Phone', null]);
  assert.equal(Backups.isImageMismatch(new Error('Runtime controller: ' + MISMATCH)), true);
  assert.equal(Backups.isImageMismatch('Stop this device first'), false);
  assert.match(Backups.restoreMessage(row(B, {image_matches:false}), 'Phone'), /different Android image/);
  assert.doesNotMatch(Backups.restoreMessage(row(A), 'Phone'), /different Android image/);
  // The controller's sentence and the GUI's must stay the same.
  assert.match(fs.readFileSync('services/runtime-backup.py', 'utf8'), /IMAGE_MISMATCH = 'This backup was made with a different Android image\.'/);
});

test('a job is followed to its end with progress, cancel and cleanup', async () => {
  const states = [job('Running'), job('Running', {done_bytes:2 * 1024 ** 3}), job('Running', {cancellable:false}), job('Succeeded')];
  const sent = [], shown = [];
  const result = await Backups.run({id:ID, name:'Phone', start:{op:'backup_create', id:ID}, sleep:async () => {},
    request:async request => { sent.push(request); return request.op === 'backup_cancel' ? report() : report({job:states.shift()}); },
    showProgress:(key, item) => shown.push([key, item]), clearProgress:key => shown.push([key, null])});
  assert.equal(result.job.status, 'Succeeded');
  assert.deepEqual(sent.map(item => item.op), ['backup_create', 'backups_get', 'backups_get', 'backups_get']);
  assert.deepEqual(shown.map(([key, item]) => [key, item && Math.round(item.percent), item && typeof item.cancel]),
    [['backup-' + ID, 25, 'function'], ['backup-' + ID, 50, 'function'], ['backup-' + ID, 25, 'undefined'], ['backup-' + ID, null, null]]);
  shown[0][1].cancel();
  await settle();
  assert.deepEqual(sent.at(-1), {op:'backup_cancel', id:ID});
});

test('failure, cancellation, replaced jobs and lost contact end the wait with a clear reason', async () => {
  const run = (states, extra = {}) => { const cleared = []; return Backups.run({id:ID, name:'Phone', start:{op:'backup_create', id:ID}, sleep:async () => {},
    request:async () => { const next = states.shift(); if (next instanceof Error) throw next; return report({job:next}); },
    clearProgress:key => cleared.push(key), ...extra}).finally(() => assert.deepEqual(cleared, ['backup-' + ID])); };
  await assert.rejects(run([job('Running'), job('Failed', {error:'Not enough free space for this backup'})]), /Not enough free space/);
  await assert.rejects(run([job('Running'), job('Cancelled')]), error => error.cancelled === true);
  await assert.rejects(run([job('Running'), job('Running', {id:'d'.repeat(32)})]), /Another operation replaced/);
  await assert.rejects(run([job('Running'), null]), /Another operation replaced/);
  // A few missed polls are tolerated; the job runs in the controller.
  const flaky = [job('Running'), Error('socket'), Error('socket'), job('Succeeded')];
  assert.equal((await run(flaky)).job.status, 'Succeeded');
  await assert.rejects(run([job('Running'), ...Array.from({length:5}, () => Error('socket closed'))]), /Lost contact.*socket closed/);
  // Nothing started: no progress to clear, and the refusal is passed on.
  await assert.rejects(Backups.run({id:ID, name:'Phone', start:{op:'backup_restore', id:ID, backup_id:A, allow_image_mismatch:false},
    request:async () => { throw new Error(MISMATCH); }}), Backups.isImageMismatch);
  await assert.rejects(Backups.run({id:ID, name:'Phone', start:{op:'backup_create', id:ID}, request:async () => report()}), /did not start/);
  await assert.rejects(Backups.run({id:ID, name:'Phone', start:{op:'backup_create', id:ID}, request:async () => report({job:job('Running', {action:'restore'})})}), /did not start/);
});

test('reinstall offers a backup only when one can be made', async () => {
  const asked = [];
  const offer = (response, answer = true) => Backups.offerBeforeReinstall({id:ID, name:'Phone',
    request:async request => { assert.deepEqual(request, {op:'backups_get', id:ID}); if (response instanceof Error) throw response; return response; },
    confirm:async (message, options) => { asked.push({message, options}); return answer; }});
  assert.equal(await offer(report()), true);
  assert.match(asked[0].message, /erases all apps, app data and Android settings on Phone[\s\S]*\/mnt\/data/);
  assert.deepEqual([asked[0].options.title, asked[0].options.okLabel, asked[0].options.cancelLabel], ['Back up Android data first?', 'Back up first', 'Skip backup']);
  assert.equal(await offer(report(), false), false);
  // A device that is running is stopped before reinstall, so "not ready" still gets the offer.
  assert.equal(await offer(report({ready:false, blocked:'Stop this device before backing up or restoring its data.'})), true);
  asked.length = 0;
  for (const unavailable of [new Error('Operation is not available'), {}, report({runtime_id:OTHER}), report({job:job('Running')}), report({limit:'This device already has 32 backups.'}), report({has_data:false})])
    assert.equal(await offer(unavailable), false);
  assert.equal(asked.length, 0);
});

test('the panel lists backups and gates actions on the device being stopped', async () => {
  const ready = panel(() => report());
  ready.tool.mount(ready.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  assert.deepEqual(ready.sent, [{op:'backups_get', id:ID}]);
  const text = ready.host.text();
  assert.match(text, /3\.00 GiB · Waydroid GAPPS · Android SDK 33/);
  assert.match(text, /Made with a different Android image/);
  assert.match(text, /Backups are stored in \/mnt\/data/);
  assert.deepEqual([...ready.buttons('Restore…'), ...ready.buttons('Delete…'), ...ready.buttons('Back up now')].map(button => button.disabled), [false, false, false, false, false]);
  // Re-rendering the same device and state does not reload; a new state does.
  ready.tool.mount(ready.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  assert.equal(ready.sent.length, 1);
  ready.tool.mount(ready.host, {id:ID, name:'Phone', state:'Running'});
  await settle();
  assert.equal(ready.sent.length, 2);

  const running = panel(() => report({ready:false, blocked:'Stop this device before backing up or restoring its data.', backups:[row(A), row(B, {available:false})]}));
  running.tool.mount(running.host, {id:ID, name:'Phone', state:'Running'});
  await settle();
  assert.match(running.host.text(), /Stop this device before backing up/);
  assert.match(running.host.text(), /File unavailable/);
  assert.deepEqual(running.buttons('Restore…').map(button => button.disabled), [true, true]);
  assert.deepEqual(running.buttons('Delete…').map(button => button.disabled), [false, false]);  // deleting needs no stopped device
  assert.equal(running.buttons('Back up now')[0].disabled, true);
  await running.buttons('Restore…')[0].click();
  await running.buttons('Back up now')[0].click();
  assert.equal(running.sent.length, 1);

  const empty = panel(() => report({backups:[]}));
  empty.tool.mount(empty.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  assert.match(empty.host.text(), /No backups yet\./);
  const fresh = panel(() => report({backups:[], has_data:false}));
  fresh.tool.mount(fresh.host, {id:ID, name:'Phone', state:'Prepared'});
  await settle();
  assert.match(fresh.host.text(), /no Android data yet/);
  assert.equal(fresh.buttons('Back up now')[0].disabled, true);
  const failed = panel(() => { throw new Error('Enable management for System Waydroid before using backups'); });
  failed.tool.mount(failed.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  assert.match(failed.host.text(), /Enable management for System Waydroid/);
  assert.equal(failed.buttons('Back up now')[0].disabled, true);
  // Another device's answer is never rendered.
  const wrong = panel(() => report({runtime_id:OTHER}));
  wrong.tool.mount(wrong.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  assert.match(wrong.host.text(), /Invalid backup response/);
  assert.equal(wrong.buttons('Restore…').length, 0);
});

test('back up now runs a job, shows progress and refreshes the list', async () => {
  const states = [job('Running'), job('Succeeded')];
  const view = panel(request => request.op === 'backup_create' || states.length < 2 && states.length ? report({job:states.shift()}) : report());
  view.tool.mount(view.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await view.buttons('Back up now')[0].click();
  await settle();
  assert.deepEqual(view.sent.map(item => item.op), ['backups_get', 'backup_create', 'backups_get', 'backups_get']);
  assert.equal(view.dialogs.length, 0);
  assert.deepEqual(view.toasts, [['success', 'Backup of Phone finished.']]);
  assert.equal(view.progress[0][1].title, 'Backing up Phone');
  assert.deepEqual(view.progress.at(-1), ['backup-' + ID, null]);
  assert.equal(view.buttons('Back up now')[0].disabled, false);

  const failing = panel(request => request.op === 'backup_create' ? report({job:job('Failed', {error:'Not enough free space for this backup'})}) : report());
  failing.tool.mount(failing.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await failing.buttons('Back up now')[0].click();
  await settle();
  assert.deepEqual(failing.toasts, [['error', 'Backup failed: Not enough free space for this backup']]);
  const cancelled = panel(request => request.op === 'backup_create' ? report({job:job('Cancelled')}) : report());
  cancelled.tool.mount(cancelled.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await cancelled.buttons('Back up now')[0].click();
  await settle();
  assert.deepEqual(cancelled.toasts, [['info', 'Backup cancelled. Nothing was changed.']]);
});

test('restore and delete are confirmed first, and a different image needs a second yes', async () => {
  const restoring = request => request.op === 'backup_restore' ? report({job:job('Succeeded', {action:'restore'})}) : report();
  const declined = panel(restoring, [false, false]);
  declined.tool.mount(declined.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await declined.buttons('Restore…')[0].click();
  await declined.buttons('Delete…')[0].click();
  await settle();
  assert.deepEqual(declined.sent.map(item => item.op), ['backups_get']);
  assert.deepEqual(declined.dialogs.map(item => item.options.title), ['Restore backup', 'Delete backup']);
  assert.match(declined.dialogs[0].message, /is replaced by the backup\. This cannot be undone\./);
  assert.match(declined.dialogs[1].message, /This cannot be undone\. The device itself is not changed\./);

  const matching = panel(restoring);
  matching.tool.mount(matching.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await matching.buttons('Restore…')[0].click();
  await settle();
  assert.deepEqual(matching.sent[1], {op:'backup_restore', id:ID, backup_id:A, allow_image_mismatch:false});
  assert.deepEqual(matching.toasts, [['success', 'Phone was restored from the backup. Start the device to use it.']]);

  // The list already says the image differs: the warning is in the one confirmation.
  const known = panel(restoring);
  known.tool.mount(known.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await known.buttons('Restore…')[1].click();
  await settle();
  assert.match(known.dialogs[0].message, /Warning: this backup was made with a different Android image/);
  assert.deepEqual(known.sent[1], {op:'backup_restore', id:ID, backup_id:B, allow_image_mismatch:true});

  // The controller found the difference itself (System Waydroid): ask again before overriding.
  for (const again of [true, false]) {
    const refused = panel(request => { if (request.op === 'backup_restore' && !request.allow_image_mismatch) throw new Error(MISMATCH); return restoring(request); }, [true, again]);
    refused.tool.mount(refused.host, {id:ID, name:'Phone', state:'Stopped'});
    await settle();
    await refused.buttons('Restore…')[0].click();
    await settle();
    assert.deepEqual(refused.dialogs.map(item => item.options.title), ['Restore backup', 'Different Android image']);
    assert.match(refused.dialogs[1].message, /different Android image[\s\S]*Restore this backup anyway\?/);
    assert.deepEqual(refused.sent.filter(item => item.op === 'backup_restore').map(item => item.allow_image_mismatch), again ? [false, true] : [false]);
    assert.equal(refused.toasts.filter(item => item[0] === 'error').length, 0);
  }

  const removing = panel(request => request.op === 'backup_delete' ? report({backups:[row(B)]}) : report());
  removing.tool.mount(removing.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await removing.buttons('Delete…')[0].click();
  await settle();
  assert.deepEqual(removing.sent[1], {op:'backup_delete', id:ID, backup_id:A});
  assert.equal(removing.buttons('Delete…').length, 1);
  assert.deepEqual(removing.toasts, [['success', 'Backup deleted.']]);

  // The file was out of reach: the entry is gone and the user is told what happened to the file.
  const unreachable = panel(request => request.op === 'backup_delete' ? report({backups:[row(B)], notice:'The backup was removed from the list, but its file could not be deleted.'}) : report());
  unreachable.tool.mount(unreachable.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await unreachable.buttons('Delete…')[0].click();
  await settle();
  assert.deepEqual(unreachable.toasts, [['info', 'The backup was removed from the list, but its file could not be deleted.']]);

  // Restored, but cleaning up failed: never shown as a failed restore.
  const warned = panel(request => request.op === 'backup_restore'
    ? report({job:job('Succeeded', {action:'restore', warning:'The device was restored from the backup, but cleaning up afterwards failed: permission denied.'})}) : report());
  warned.tool.mount(warned.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  await warned.buttons('Restore…')[0].click();
  await settle();
  assert.deepEqual(warned.toasts, [['info', 'The device was restored from the backup, but cleaning up afterwards failed: permission denied.']]);
  assert.throws(() => Backups.validate(report({notice:5}), ID), /Invalid backup response/);
  assert.throws(() => Backups.validate(report({job:job('Succeeded', {warning:{}})}), ID), /Invalid backup response/);
});

test('closing the section stops updates for the old device', async () => {
  let release;
  const view = panel(() => new Promise(resolve => { release = () => resolve(report()); }));
  view.tool.mount(view.host, {id:ID, name:'Phone', state:'Stopped'});
  await settle();
  view.tool.deactivate();
  release();
  await settle();
  assert.equal(view.host.children.length, 0);
  assert.equal(view.buttons('Restore…').length, 0);
});

test('the feature is wired into the app without unsafe markup or blocking dialogs', () => {
  const source = fs.readFileSync('crates/anvildroid-gui/src-frontend/runtime-backups.js', 'utf8');
  assert.doesNotMatch(source, /innerHTML|window\.confirm|alert\(/);
  const main = fs.readFileSync('crates/anvildroid-gui/src-frontend/main.js', 'utf8');
  const html = fs.readFileSync('crates/anvildroid-gui/src-frontend/index.html', 'utf8');
  assert.ok(html.indexOf('<script src="runtime-backups.js" defer></script>') < html.indexOf('<script src="main.js" defer></script>'));
  assert.match(main, /\['backups','runtime-backups'\]/);
  assert.equal(main.split('<summary>Backups</summary><div class="runtime-backups-panel"><div data-runtime-backups="${escapeHtml(record.id)}"></div>').length, 3);  // managed devices and System Waydroid
  const offer = main.indexOf('offerBeforeReinstall('), confirm = main.indexOf("{title:'Reinstall device',kind:'warning'}");
  const stop = main.indexOf('await RuntimeSettings.prepareReinstall('), backup = main.indexOf("start:{op:'backup_create', id:request.id}");
  const erase = main.indexOf('await RuntimeSettings.apply(request, {');
  assert.ok(offer > 0 && offer < confirm && confirm < stop && stop < backup && backup < erase, 'offer → confirm → stop → backup → reinstall');
  const tabs = require('../crates/anvildroid-gui/src-frontend/settings-tabs.js');
  assert.deepEqual(tabs.order(['backups', 'diagnostics', 'storage']), ['storage', 'backups', 'diagnostics']);
  const css = fs.readFileSync('crates/anvildroid-gui/src-frontend/layout.css', 'utf8');
  assert.match(css, /\.backup-row \{/);
});
