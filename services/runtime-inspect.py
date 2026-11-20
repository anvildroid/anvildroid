"""Bounded, fixed-argv queries inside a running device's Android.

Feature modules (logs, capture, proxy, performance) each declare

    FIELDS  = {op: exact request field set}
    QUERIES = {kind: function(run, params, sink) -> JSON value}
    dispatch(store, request, uid, query) -> JSON value

A query function runs where the container is reachable: in the controller
for System Waydroid, in the runtime worker for a managed device (its LXC
command socket lives in the worker's network namespace). It must validate
`params` itself, because both paths call it. `run` executes one Android tool
by fixed argv, never through a shell. `sink` is a controller-owned file
descriptor for large binary output, or None.

A query never wakes a sleeping device: it answers only while Android runs.
"""
import collections
import contextlib
import importlib.util
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import threading
import time

FEATURES = ('runtime-logs', 'runtime-capture', 'runtime-proxy', 'runtime-performance')
TOOL = re.compile(r'[a-z][a-z0-9_]{0,31}')
MAX_TIMEOUT = 20
MAX_TEXT = 768 * 1024           # a worker answers in one JSON line of at most 1 MiB
MAX_SINK = 64 * 1024**2
ASLEEP = 'This device is asleep. Open an app or the Android window, then retry.'
STOPPED = 'Start this device first.'
OUTDATED = 'Stop and start this device once to load the updated device tools.'
CROWDED = 'This device is still answering earlier requests. Try again in a moment.'
MAX_WAITING = 3                 # queries of one device that may run or wait at the same time
waiting, waiting_lock = collections.Counter(), threading.Lock()


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


features = [module(name) for name in FEATURES]
FIELDS, QUERIES, OWNERS = {}, {}, {}
for feature in features:
    for op, fields in feature.FIELDS.items():
        if op in FIELDS or not {'op', 'id'} <= set(fields):
            raise RuntimeError('Invalid or duplicate device tool operation: ' + op)
        FIELDS[op] = set(fields)
        OWNERS[op] = feature
    for kind, function in feature.QUERIES.items():
        if kind in QUERIES or not TOOL.fullmatch(kind):
            raise RuntimeError('Invalid or duplicate device query: ' + kind)
        QUERIES[kind] = function


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def runner(lxc_path, name, popen=subprocess.Popen):
    """Return run(tool, *args, ...) for one container.

    run() returns the tool's stdout as bytes, or the byte count when `sink`
    is given. `tail=True` keeps the last `limit` bytes instead of failing on
    longer output. A non-zero exit raises unless `check=False`, in which case
    the result is (returncode, stdout)."""
    def run(tool, *args, timeout=8, limit=256 * 1024, sink=None, tail=False, check=True):
        require(isinstance(tool, str) and TOOL.fullmatch(tool), 'Invalid Android tool')
        require(len(args) <= 64 and all(isinstance(a, str) and a and '\0' not in a and len(a) <= 4096 for a in args),
                'Invalid Android tool arguments')
        require(type(timeout) in (int, float) and 0 < timeout <= MAX_TIMEOUT, 'Invalid Android tool timeout')
        require(type(limit) is int and 0 < limit <= (MAX_SINK if sink is not None else MAX_TEXT),
                'Invalid Android tool output limit')
        argv = ['lxc-attach', '--set-var', 'PATH=/system/bin:/system/xbin:/vendor/bin', '-P', str(lxc_path),
                '-n', name, '--', '/system/bin/' + tool, *args]
        deadline = time.monotonic() + timeout
        process = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        start_new_session=True, umask=0o022)

        def kill():
            # A timed-out attach must not leave children holding LXC control descriptors.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

        chunks, kept, total, complaint = collections.deque(), 0, 0, b''
        try:
            source, errors = process.stdout.fileno(), process.stderr.fileno()
            watched = [source, errors]
            while source in watched:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f'Android {tool} timed out')
                ready = select.select(watched, [], [], remaining)[0]
                if errors in ready:
                    # Drained and mostly discarded: the tool comes from the device's image.
                    text = os.read(errors, 65536)
                    complaint = (complaint + text)[-512:]
                    if not text:
                        watched.remove(errors)
                if source not in ready:
                    continue
                part = os.read(source, 65536)
                if not part:
                    break
                total += len(part)
                if sink is not None:
                    require(total <= limit, f'Android {tool} output is too large')
                    view = memoryview(part)
                    while view:
                        view = view[os.write(sink, view):]
                elif tail:
                    chunks.append(part)
                    kept += len(part)
                    while kept - len(chunks[0]) >= limit:
                        kept -= len(chunks.popleft())
                else:
                    require(total <= limit, f'Android {tool} output is too large')
                    chunks.append(part)
            try:
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise RuntimeError(f'Android {tool} timed out') from None
            while errors in watched and select.select([errors], [], [], 0)[0]:
                text = os.read(errors, 65536)        # what the tool said last, for the error message
                if not text:
                    break
                complaint = (complaint + text)[-512:]
        except BaseException:
            kill()
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        output = b''.join(chunks)[-limit:] if sink is None else total
        if not check:
            return code, output
        if code != 0:
            detail = complaint.decode(errors='replace').strip()
            if not detail and sink is None:
                detail = output[-512:].decode(errors='replace').strip()
            raise RuntimeError(f'Android {tool} failed: ' + (detail or f'exit code {code}'))
        return output
    return run


def answer(kind, params, sink, run):
    require(isinstance(kind, str) and kind in QUERIES, 'Unknown device query')
    require(isinstance(params, dict), 'Invalid device query parameters')
    return QUERIES[kind](run, params, sink)


def worker_request(request, sink, generation, lxc_path, name):
    """Answer one {'op': 'inspect', ...} request inside the runtime worker."""
    require(isinstance(request, dict) and set(request) == {'op', 'kind', 'params', 'generation'},
            'Invalid device query')
    require(request['generation'] == generation, 'Runtime generation changed')
    return answer(request['kind'], request['params'], sink, runner(lxc_path, name))


def query(store, identifier, uid, record, kind, params, sink=None):
    """Run one query for an already authorized device and return its result."""
    require(kind in QUERIES and isinstance(params, dict), 'Unknown device query')
    # One device answers one query at a time. Without a cap, repeated requests
    # would queue up and hold the controller's request threads.
    with waiting_lock:
        require(waiting[identifier] < MAX_WAITING, CROWDED)
        waiting[identifier] += 1
    try:
        return ask(store, identifier, uid, record, kind, params, sink)
    finally:
        with waiting_lock:
            waiting[identifier] -= 1
            if not waiting[identifier]:
                del waiting[identifier]


def ask(store, identifier, uid, record, kind, params, sink):
    if identifier == 'default':
        session, _ = store.existing.authorize(uid)
        state = store.existing.state()
        require(state != 'FROZEN', ASLEEP)
        require(state == 'RUNNING' and isinstance(session, dict), STOPPED)
        with store.unlocked():
            result = answer(kind, params, sink, runner(store.existing.work / 'lxc', 'waydroid'))
        after, _ = store.existing.authorize(uid)
        require(isinstance(after, dict) and after.get('pid') == session.get('pid')
                and after.get('user_id') == session.get('user_id'),
                'System Waydroid changed during this action; retry')
        return result
    require(record.get('state') != 'Frozen', ASLEEP)
    require(record.get('state') == 'Running', STOPPED)
    with store.worker_rpc(identifier):
        status = store.android.status(identifier)
        require(status.get('state') != 'Frozen', ASLEEP)
        require(status.get('state') == 'Running', STOPPED)
        require('inspect' in status.get('capabilities', []), OUTDATED)
        report = store.android.status(identifier, 'inspect', {
            'op': 'inspect', 'kind': kind, 'params': params, 'generation': status.get('generation')}, apk_fd=sink)
    if report.get('inspect_error'):
        raise RuntimeError(report['inspect_error'])
    require('inspect_result' in report, 'Device did not answer the query')
    return report['inspect_result']


def dispatch(store, request, uid, record, descriptor=None):
    """Route one feature request. `record` comes from the caller's authorize()."""
    op = request['op']
    identifier = request['id']

    def bound(kind, params, sink=None):
        return query(store, identifier, uid, record, kind, params, sink)
    feature = OWNERS[op]
    if getattr(feature, 'DESCRIPTOR_OPS', None) and op in feature.DESCRIPTOR_OPS:
        require(isinstance(descriptor, int), 'This action needs a destination file')
        return feature.dispatch(store, request, uid, bound, descriptor)
    require(descriptor is None, 'Unexpected file descriptor')
    return feature.dispatch(store, request, uid, bound)
