// Screenshot section of Device settings: pick a file, save a PNG of the Android display.
// The app opens the chosen file itself; the controller only ever gets a descriptor.
(function(root) {
  const WAITING = {
    Frozen: 'This device is asleep. Open an app or the Android window, then save a screenshot.',
    Starting: 'This device is starting. You can save a screenshot when it is running.',
  };
  const two = value => String(value).padStart(2, '0');

  // Device names are free text: keep what every file system accepts.
  function safeName(name) {
    const clean = String(name ?? '').replace(/[\u0000-\u001f\u007f<>:"/\\|?*]+/g, ' ').replace(/\s+/g, ' ')
      .replace(/^[ .]+|[ .]+$/g, '').slice(0, 60).trim();
    return clean || 'device';
  }
  function defaultFileName(name, date = new Date()) {
    const day = `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`;
    const time = `${two(date.getHours())}.${two(date.getMinutes())}.${two(date.getSeconds())}`;
    return `AnvilDroid ${safeName(name)} ${day} ${time}.png`;
  }
  function validate(value, id) {
    const side = number => Number.isSafeInteger(number) && number > 0 && number <= 16384;
    if (!value || value.runtime_id !== id || !Number.isSafeInteger(value.bytes) || value.bytes <= 0 || !side(value.width) || !side(value.height))
      throw new Error('The controller did not confirm a screenshot of this device.');
    return value;
  }

  function create({invoke, document, toast = () => {}, dialog, now = () => new Date()}) {
    function element(tag, text, parent, className) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      if (parent) parent.append(node);
      return node;
    }
    const panel = element('section', undefined, null, 'runtime-inline-feature runtime-capture');
    element('p', 'Save a PNG picture of the whole Android display. With separate app windows, the picture shows Android’s own desktop arrangement. It can differ from the windows on your Linux desktop.', panel, 'hint');
    const save = element('button', 'Save screenshot…', panel, 'btn btn-primary'); save.type = 'button';
    const waiting = element('p', '', panel, 'hint capture-waiting');
    const notice = element('p', '', panel); notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
    let runtime = null, epoch = 0, busy = false;

    function say(text, error = false) {
      notice.textContent = text;
      notice.className = error ? 'runtime-feedback error' : '';
    }
    function update() {
      const running = runtime?.state === 'Running';
      save.disabled = busy || !running;
      save.textContent = busy ? 'Saving…' : 'Save screenshot…';
      waiting.textContent = !runtime || running ? '' : WAITING[runtime.state] || 'Start this device to save a screenshot.';
    }
    save.addEventListener('click', async () => {
      if (!runtime || busy || runtime.state !== 'Running') return;
      const current = epoch, id = runtime.id;
      busy = true; update();
      try {
        const path = await dialog.save({defaultPath:defaultFileName(runtime.name, now()), title:'Save screenshot',
          filters:[{name:'PNG image', extensions:['png']}]});
        // The section was closed or shows another device now: never save the wrong one.
        if (!path || current !== epoch) return;
        say('Taking the screenshot…');
        const result = validate(await invoke('save_device_screenshot', {id, path}), id);
        if (current !== epoch) return;
        say(`Saved ${result.width} × ${result.height} screenshot to ${path}`);
        toast('Screenshot saved.', 'success');
      } catch (error) {
        if (current === epoch) say(`The screenshot was not saved. ${String(error?.message || error)}`, true);
      } finally {
        if (current === epoch) { busy = false; update(); }
      }
    });
    update();
    return {
      mount(host, value) {
        if (panel.parentNode !== host) host.append(panel);
        // The page re-renders often: only another device resets what is shown.
        if (runtime?.id !== value.id) { epoch++; busy = false; say(''); }
        runtime = {id:value.id, name:value.name, state:value.state};
        panel.dataset.runtimeId = value.id;
        update();
      },
      deactivate() {
        if (runtime) epoch++;
        runtime = null; busy = false; say(''); update(); panel.remove();
      },
    };
  }
  const api = {create, defaultFileName, safeName, validate};
  root.AnvilRuntimeCapture = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
