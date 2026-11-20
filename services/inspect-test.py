#!/usr/bin/env python3
"""Inert tests for bounded Android queries (runtime-inspect.py)."""
import contextlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('runtime_inspect', Path(__file__).with_name('runtime-inspect.py'))
inspect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspect)


def shell(script, seen=None):
    """A Popen stand-in that runs `script` instead of lxc-attach."""
    def popen(argv, **options):
        if seen is not None:
            seen.append(argv)
        return subprocess.Popen(['/bin/sh', '-c', script], **options)
    return popen


class Runner(unittest.TestCase):
    def run_script(self, script, *args, seen=None, **options):
        return inspect.runner(Path('/var/lib/x/lxc'), 'waydroid', popen=shell(script, seen))(*args, **options)

    def test_fixed_argv_without_a_shell(self):
        seen = []
        self.assertEqual(self.run_script('printf hi', 'logcat', '-d', '-t', '5', seen=seen), b'hi')
        self.assertEqual(seen[0], ['lxc-attach', '--set-var', 'PATH=/system/bin:/system/xbin:/vendor/bin',
                                   '-P', '/var/lib/x/lxc', '-n', 'waydroid', '--', '/system/bin/logcat', '-d', '-t', '5'])

    def test_rejects_unsafe_tools_arguments_and_limits(self):
        for tool in ('../sh', 'sh -c', '', 'Logcat', '/system/bin/sh', 5):
            with self.assertRaisesRegex(RuntimeError, 'Invalid Android tool'):
                self.run_script('true', tool)
        for bad in (('a\0b',), ('',), (5,), ('x',) * 65, ('y' * 4097,)):
            with self.assertRaisesRegex(RuntimeError, 'arguments'):
                self.run_script('true', 'logcat', *bad)
        for options in ({'timeout': 0}, {'timeout': 21}, {'timeout': True}, {'limit': 0},
                        {'limit': inspect.MAX_TEXT + 1}, {'limit': 1.5}):
            with self.assertRaisesRegex(RuntimeError, 'Invalid Android tool'):
                self.run_script('true', 'logcat', **options)

    def test_output_limit_fails_or_keeps_the_tail(self):
        script = 'i=0; while [ $i -lt 2000 ]; do printf "line %05d\\n" $i; i=$((i+1)); done'
        with self.assertRaisesRegex(RuntimeError, 'too large'):
            self.run_script(script, 'logcat', limit=1000)
        tail = self.run_script(script, 'logcat', limit=1000, tail=True)
        self.assertEqual(len(tail), 1000)
        self.assertTrue(tail.endswith(b'line 01999\n'))

    def test_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'late'
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                self.run_script(f'(sleep 2; touch {marker}) & sleep 30', 'screencap', timeout=0.3)
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(2.2)
            self.assertFalse(marker.exists())

    def test_timeout_also_covers_a_tool_that_closed_stdout(self):
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            self.run_script('exec >&-; sleep 30', 'dumpsys', timeout=0.3)

    def test_failure_reports_stderr_then_stdout(self):
        with self.assertRaisesRegex(RuntimeError, 'Android settings failed: no such setting'):
            self.run_script('echo no such setting >&2; exit 3', 'settings')
        with self.assertRaisesRegex(RuntimeError, 'Android settings failed: from stdout'):
            self.run_script('echo from stdout; exit 3', 'settings')
        with self.assertRaisesRegex(RuntimeError, 'exit code 4'):
            self.run_script('exit 4', 'settings')
        self.assertEqual(self.run_script('echo x; exit 3', 'settings', check=False), (3, b'x\n'))

    def test_noisy_stderr_is_drained_and_only_its_end_is_kept(self):
        script = 'head -c 3000000 /dev/zero | tr "\\0" e >&2; echo last words >&2; echo fine'
        self.assertEqual(self.run_script(script, 'logcat'), b'fine\n')
        with self.assertRaisesRegex(RuntimeError, 'Android logcat failed: e+last words$') as caught:
            self.run_script(script + '; exit 2', 'logcat')
        self.assertLess(len(str(caught.exception)), 600)

    def test_sink_receives_binary_output_with_a_cap(self):
        with tempfile.TemporaryFile() as sink:
            count = self.run_script('head -c 300000 /dev/zero', 'screencap', sink=sink.fileno(), limit=400000)
            self.assertEqual(count, 300000)
            self.assertEqual(os.fstat(sink.fileno()).st_size, 300000)
        with tempfile.TemporaryFile() as sink, self.assertRaisesRegex(RuntimeError, 'too large'):
            self.run_script('head -c 300000 /dev/zero', 'screencap', sink=sink.fileno(), limit=1000)


class Existing:
    def __init__(self, state='RUNNING', owner=1000):
        self.work, self.current, self.owner = Path('/var/lib/waydroid'), state, owner
        self.session = {'pid': '41', 'user_id': str(owner)}

    def authorize(self, uid):
        if uid != self.owner:
            raise RuntimeError('System Waydroid belongs to another user')
        return dict(self.session), {}

    def state(self):
        return self.current


class Android:
    def __init__(self, state='Running', capabilities=('inspect',)):
        self.state, self.capabilities, self.calls = state, list(capabilities), []
        self.answer = {'inspect_result': {'ok': 1}}

    def status(self, identifier, operation='status', payload=None, apk_fd=None):
        self.calls.append((identifier, operation, payload, apk_fd))
        report = {'state': self.state, 'capabilities': self.capabilities, 'generation': 'a' * 32}
        return dict(report, **self.answer) if operation == 'inspect' else report


class Store:
    def __init__(self, existing=None, android=None):
        self.existing, self.android = existing or Existing(), android or Android()
        self.unlocked = contextlib.nullcontext
        self.worker_rpc = lambda identifier: contextlib.nullcontext()


def fake_feature(descriptor=False):
    feature = types.SimpleNamespace(FIELDS={'probe_get': {'op', 'id', 'count'}}, QUERIES={})
    if descriptor:
        feature.DESCRIPTOR_OPS = {'probe_get'}
        feature.dispatch = lambda store, request, uid, query, fd: query('probe', {'count': request['count']}, fd)
    else:
        feature.dispatch = lambda store, request, uid, query: query('probe', {'count': request['count']})
    return feature


class Queries(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def probe(run, params, sink):
            self.seen.append((params, sink))
            return {'count': params['count']}
        patches = (mock.patch.dict(inspect.QUERIES, {'probe': probe}),
                   mock.patch.object(inspect, 'runner', lambda path, name: (path, name)))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_system_waydroid_answers_only_while_running(self):
        self.assertEqual(inspect.query(Store(), 'default', 1000, {}, 'probe', {'count': 2}), {'count': 2})
        with self.assertRaisesRegex(RuntimeError, 'asleep'):
            inspect.query(Store(Existing('FROZEN')), 'default', 1000, {}, 'probe', {})
        with self.assertRaisesRegex(RuntimeError, 'Start this device'):
            inspect.query(Store(Existing('STOPPED')), 'default', 1000, {}, 'probe', {})
        with self.assertRaisesRegex(RuntimeError, 'another user'):
            inspect.query(Store(), 'default', 1001, {}, 'probe', {})
        self.assertEqual(len(self.seen), 1)

    def test_system_waydroid_session_change_discards_the_answer(self):
        existing = Existing()

        def probe(run, params, sink):
            existing.session = {'pid': '99', 'user_id': '1000'}
            return {}
        with mock.patch.dict(inspect.QUERIES, {'probe': probe}), self.assertRaisesRegex(RuntimeError, 'changed'):
            inspect.query(Store(existing), 'default', 1000, {}, 'probe', {})

    def test_managed_device_goes_through_its_worker(self):
        android = Android()
        store = Store(android=android)
        identifier = 'r-' + '1' * 32
        self.assertEqual(inspect.query(store, identifier, 1000, {'state': 'Running'}, 'probe', {'count': 1}, 7), {'ok': 1})
        self.assertEqual(android.calls[-1], (identifier, 'inspect', {
            'op': 'inspect', 'kind': 'probe', 'params': {'count': 1}, 'generation': 'a' * 32}, 7))
        self.assertEqual(self.seen, [])   # the controller never attaches to a managed container
        android.answer = {'inspect_error': 'Android logcat timed out'}
        with self.assertRaisesRegex(RuntimeError, 'logcat timed out'):
            inspect.query(store, identifier, 1000, {'state': 'Running'}, 'probe', {})
        android.answer = {}
        with self.assertRaisesRegex(RuntimeError, 'did not answer'):
            inspect.query(store, identifier, 1000, {'state': 'Running'}, 'probe', {})

    def test_managed_device_states_and_old_workers(self):
        identifier = 'r-' + '1' * 32
        for record, android, message in (
                ({'state': 'Frozen'}, Android(), 'asleep'),
                ({'state': 'Stopped'}, Android(), 'Start this device'),
                ({'state': 'Running'}, Android('Frozen'), 'asleep'),
                ({'state': 'Running'}, Android('Stopping'), 'Start this device'),
                ({'state': 'Running'}, Android(capabilities=()), 'Stop and start this device once')):
            with self.assertRaisesRegex(RuntimeError, message):
                inspect.query(Store(android=android), identifier, 1000, record, 'probe', {})
            self.assertFalse(any(call[1] == 'inspect' for call in android.calls))

    def test_one_device_cannot_queue_requests_without_bound(self):
        with mock.patch.dict(inspect.waiting, {'default': inspect.MAX_WAITING}):
            with self.assertRaisesRegex(RuntimeError, 'still answering'):
                inspect.query(Store(), 'default', 1000, {}, 'probe', {})
            other = 'r-' + '2' * 32          # another device is not affected
            self.assertEqual(inspect.query(Store(), other, 1000, {'state': 'Running'}, 'probe', {}), {'ok': 1})
        self.assertEqual(self.seen, [])
        with self.assertRaisesRegex(RuntimeError, 'asleep'):
            inspect.query(Store(Existing('FROZEN')), 'default', 1000, {}, 'probe', {})
        self.assertNotIn('default', inspect.waiting)     # released after a failure too

    def test_unknown_queries_and_parameters(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown device query'):
            inspect.query(Store(), 'default', 1000, {}, 'missing', {})
        with self.assertRaisesRegex(RuntimeError, 'Unknown device query'):
            inspect.query(Store(), 'default', 1000, {}, 'probe', [])

    def test_worker_request_checks_shape_and_generation(self):
        request = {'op': 'inspect', 'kind': 'probe', 'params': {'count': 3}, 'generation': 'g'}
        self.assertEqual(inspect.worker_request(request, None, 'g', Path('/x'), 'r-1'), {'count': 3})
        with self.assertRaisesRegex(RuntimeError, 'generation changed'):
            inspect.worker_request(request, None, 'other', Path('/x'), 'r-1')
        for bad in (dict(request, extra=1), {'op': 'inspect'}, dict(request, kind='missing'),
                    dict(request, params='x'), dict(request, kind=['probe'])):
            with self.assertRaises(RuntimeError):
                inspect.worker_request(bad, None, 'g', Path('/x'), 'r-1')

    def test_dispatch_file_descriptor_rules(self):
        request = {'op': 'probe_get', 'id': 'default', 'count': 4}
        plain, with_fd = fake_feature(), fake_feature(descriptor=True)
        with mock.patch.dict(inspect.OWNERS, {'probe_get': plain}):
            self.assertEqual(inspect.dispatch(Store(), request, 1000, {}), {'count': 4})
            with self.assertRaisesRegex(RuntimeError, 'Unexpected file descriptor'):
                inspect.dispatch(Store(), request, 1000, {}, 9)
        with mock.patch.dict(inspect.OWNERS, {'probe_get': with_fd}):
            self.assertEqual(inspect.dispatch(Store(), request, 1000, {}, 9), {'count': 4})
            self.assertEqual(self.seen[-1], ({'count': 4}, 9))
            with self.assertRaisesRegex(RuntimeError, 'needs a destination file'):
                inspect.dispatch(Store(), request, 1000, {})


class Registry(unittest.TestCase):
    def test_every_operation_names_a_device_and_has_one_owner(self):
        for op, fields in inspect.FIELDS.items():
            self.assertLessEqual({'op', 'id'}, fields, op)
            self.assertIn(op, inspect.OWNERS)
        self.assertEqual(len(inspect.features), len(inspect.FEATURES))


if __name__ == '__main__':
    unittest.main()
