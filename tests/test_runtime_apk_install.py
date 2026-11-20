"""Split packages on the managed path: one caller-opened file, APK sizes, one session.
The controller case needs root (real store ownership checks); the rest runs as any user.
Run: python3 -m unittest discover -s tests -p test_runtime_apk_install.py
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location('apk_install_' + name, ROOT/'services'/f'runtime-{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
transfer, worker, android = load('transfer'), load('worker'), load('android')
linux = worker.linux

BASE, ARM, DPI = b'PK\x03\x04base', b'PK\x03\x04arm64-v8a', b'PK\x03\x04xxhdpi'
SIZES = [len(BASE), len(ARM), len(DPI)]


def package(*apks):
    file = tempfile.TemporaryFile()
    file.write(b''.join(apks)); file.flush()
    return file


class FakeAndroid:
    """Stands in for lxc-attach + pm; records what Android would receive."""
    def __init__(self, test, instance, replies=None):
        self.test, self.instance, self.replies = test, instance, replies or {}
        self.calls, self.received = [], []

    def __call__(self, *args, **kwargs):
        args = list(map(str, args))
        if args[6] == '/system/bin/sh':
            # The only shell text the installer may run: the fixed session listing.
            self.test.assertEqual(args[:6] + args[7:], ['lxc-attach', '-P', str(self.instance/'android'), '-n', 'r-two', '--', '-c', linux.PACKAGE_SESSIONS])
            self.calls.append(['sessions'])
            reply = self.replies.get('sessions', 'Active install sessions:\n')
            if isinstance(reply, Exception): raise reply
            return reply
        self.test.assertEqual(args[:7], ['lxc-attach', '-P', str(self.instance/'android'), '-n', 'r-two', '--', '/system/bin/pm'])
        command = args[7:]
        self.calls.append(command)
        self.test.assertEqual(kwargs['operation'], {'install-create': 'APK session', 'install-write': 'APK transfer',
            'install-commit': 'APK installation', 'install-abandon': 'APK session cleanup'}[command[0]])
        self.test.assertGreater(kwargs['timeout'], 0)
        reply = self.replies.get(command[0] if command[0] != 'install-write' else command[-1])
        if isinstance(reply, Exception): raise reply
        if command[0] == 'install-create': return reply or 'Success: created install session [1234]'
        if command[0] == 'install-write':
            self.test.assertEqual(command[1], '-S')
            # pm reads exactly the announced size from the shared file offset.
            self.received.append((command[3], command[4], os.read(kwargs['stdin'].fileno(), int(command[2]))))
            return reply or 'Success: streamed %s bytes' % command[2]
        return reply or 'Success'


class SplitInstallTests(unittest.TestCase):
    def test_sizes_must_describe_the_open_file_exactly(self):
        with package(BASE, ARM, DPI) as file:
            self.assertEqual(transfer.apk_parts(file.fileno(), list(SIZES)), SIZES)
            for parts in ([sum(SIZES)], [], None, 'x', [8, 13, '10'], [8, 13, 10.0], [True, 30], [8, 0, 23], [8, -1, 24],
                          [8, 13], [8, 13, 11], [9, 12, 10], [7, 14, 10], list(SIZES) + [1], [1] * 513):
                with self.subTest(parts=parts), self.assertRaises(ValueError):
                    transfer.apk_parts(file.fileno(), parts)
        with self.assertRaises(ValueError): transfer.apk_parts(None, SIZES)
        read, write = os.pipe()
        try:
            with self.assertRaisesRegex(ValueError, 'regular'): transfer.apk_parts(read, SIZES)
        finally: os.close(read); os.close(write)
        with tempfile.NamedTemporaryFile() as named:
            named.write(BASE + ARM + DPI); named.flush()
            fd = os.open(named.name, os.O_WRONLY)
            try:
                with self.assertRaisesRegex(ValueError, 'readable'): transfer.apk_parts(fd, list(SIZES))
            finally: os.close(fd)

    def test_worker_installs_every_apk_in_one_session(self):
        with tempfile.TemporaryDirectory() as directory, package(BASE, ARM, DPI) as file:
            instance = Path(directory)
            fake = FakeAndroid(self, instance)
            with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                result = worker.install_apk(instance, 'r-two', file.fileno(), parts=list(SIZES))
            self.assertEqual(result['action'], 'install')
            self.assertTrue(result['success'])
            self.assertEqual(fake.calls[0], ['install-create', '-r', '--user', '0', '-S', str(sum(SIZES))])
            self.assertEqual(fake.received, [('1234', '0.apk', BASE), ('1234', '1.apk', ARM), ('1234', '2.apk', DPI)])
            self.assertEqual(fake.calls[-1], ['install-commit', '1234'])
            self.assertEqual(len(fake.calls), 5)
            self.assertEqual(list(instance.iterdir()), [])  # the snapshot is gone

    def test_failure_before_commit_abandons_the_session_and_says_nothing_was_installed(self):
        for reply in ('Error: java.lang.IllegalArgumentException', RuntimeError('APK transfer command timed out'),
                      RuntimeError('APK transfer failed: Failure [INSTALL_FAILED_INVALID_APK]')):
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as directory, package(BASE, ARM, DPI) as file:
                fake = FakeAndroid(self, Path(directory), {'1.apk': reply})
                with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                    with self.assertRaisesRegex(RuntimeError, r'Nothing was installed\.$') as raised:
                        worker.install_apk(Path(directory), 'r-two', file.fileno(), parts=list(SIZES))
                self.assertNotIn('outcome is unknown', str(raised.exception))
                self.assertEqual([call[0] for call in fake.calls], ['install-create', 'install-write', 'install-write', 'install-abandon'])
                self.assertEqual(fake.calls[-1], ['install-abandon', '1234'])

    def test_a_session_android_may_have_opened_is_never_left_behind(self):
        now = int(time.time() * 1000)
        def session(number, uid, created, committed='false'):
            return (f'  Active Session {number}:\n    userId=0 mOriginalInstallerUid={uid} mInstallerUid={uid}\n'
                    f'    createdMillis={created} updatedMillis={created} committedMillis=0\n'
                    f'    mCommitted={committed} mSealed={committed} mDestroyed=false\n')
        listing = ('Active install sessions:\n' + session(11, 0, now) + session(12, 2000, now) + session(13, 0, now - 3600_000)
                   + session(14, 0, now, 'true') + session(15, 10123, now) + 'Finalized install sessions:\n' + session(16, 0, now))
        self.assertEqual(linux.open_sessions(listing, 0, now - 1000), {'11'})
        self.assertEqual(linux.open_sessions('unknown text', 0, 0), set())
        cases = [
            # The create command failed or timed out: the number is unknown, so it is looked up.
            ({'install-create': RuntimeError('APK session command timed out'), 'sessions': listing}, ['install-create', 'sessions', 'install-abandon'], ['install-abandon', '11']),
            ({'install-create': RuntimeError('APK session failed: Broken pipe'), 'sessions': listing}, ['install-create', 'sessions', 'install-abandon'], ['install-abandon', '11']),
            ({'install-create': 'Error: no space'}, ['install-create', 'sessions'], None),
            ({'install-create': 'Success: no session number', 'sessions': RuntimeError('APK session cleanup command timed out')}, ['install-create', 'sessions'], None),
            # A session number without a Success line is abandoned by its number.
            ({'install-create': 'Failure: created install session [77] and then failed'}, ['install-create', 'install-abandon'], ['install-abandon', '77']),
        ]
        for replies, calls, last in cases:
            with self.subTest(replies=replies), tempfile.TemporaryDirectory() as directory, package(BASE, ARM, DPI) as file:
                fake = FakeAndroid(self, Path(directory), replies)
                with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                    with self.assertRaisesRegex(RuntimeError, r'Nothing was installed\.$') as raised:
                        worker.install_apk(Path(directory), 'r-two', file.fileno(), parts=list(SIZES))
                self.assertNotIn('outcome is unknown', str(raised.exception))
                self.assertEqual([call[0] for call in fake.calls], calls)
                if last: self.assertEqual(fake.calls[-1], last)

    def test_warnings_before_success_do_not_fail_a_step(self):
        with tempfile.TemporaryDirectory() as directory, package(BASE, ARM, DPI) as file:
            fake = FakeAndroid(self, Path(directory), {
                'install-create': 'WARNING: linker: unsupported flags\nSuccess: created install session [1234]',
                '0.apk': 'WARNING: linker: unsupported flags\nSuccess: streamed 8 bytes',
                'install-commit': 'WARNING: linker: unsupported flags\nSuccess'})
            with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                self.assertTrue(worker.install_apk(Path(directory), 'r-two', file.fileno(), parts=list(SIZES))['success'])
            self.assertEqual(fake.calls[-1], ['install-commit', '1234'])
            self.assertEqual(len(fake.received), 3)

    def test_a_failing_window_switch_before_commit_abandons_the_session(self):
        for failure in (RuntimeError('lxc-attach failed: no container'), subprocess.TimeoutExpired('lxc-attach', 8)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, package(BASE, ARM) as file:
                fake = FakeAndroid(self, Path(directory))
                with patch.object(worker.linux, 'run_bounded', side_effect=fake), patch.object(worker, 'show_android_ui', side_effect=failure):
                    with self.assertRaisesRegex(RuntimeError, r'Nothing was installed\.$'):
                        worker.install_apk(Path(directory), 'r-two', file.fileno(), desktop=True, parts=[len(BASE), len(ARM)])
                self.assertEqual([call[0] for call in fake.calls], ['install-create', 'install-write', 'install-write', 'install-abandon'])
                self.assertEqual(fake.calls[-1], ['install-abandon', '1234'])

    def test_the_callers_file_is_copied_by_a_bounded_child_and_checked_as_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory)
            def refused(file, parts, pattern, **patches):
                fake = FakeAndroid(self, instance)
                with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                    with self.assertRaisesRegex(RuntimeError, pattern):
                        worker.install_apk(instance, 'r-two', file if isinstance(file, int) else file.fileno(), parts=parts)
                self.assertEqual(fake.calls, [])            # Android never saw it
                self.assertEqual(list(instance.iterdir()), [])  # and nothing is left on disk
            # Longer or shorter than announced, or not APKs at the announced borders.
            with package(BASE, ARM, DPI, b'extra') as file: refused(file, list(SIZES), 'exactly the announced APK sizes.*Nothing was installed')
            with package(BASE, ARM) as file: refused(file, list(SIZES), 'exactly the announced APK sizes')
            with package(BASE, b'XX\x03\x04arm64-v8a', DPI) as file: refused(file, list(SIZES), 'not an APK archive')
            with package(b'not an apk') as file: refused(file, None, 'not an APK archive')
            with package() as file: refused(file, None, 'between 1 byte and 2 GiB')
            # Something that is not a file is refused without reading it.
            read, write = os.pipe()
            try: refused(read, list(SIZES), 'must be a regular file')
            finally: os.close(read); os.close(write)
            # A disk that is nearly full: refused before copying, with the amount needed.
            with package(BASE, ARM, DPI) as file, patch.object(worker, 'free_bytes', return_value=worker.SNAPSHOT_RESERVE + 5):
                refused(file, list(SIZES), 'Not enough free space.*1 MB are needed')
            with package(BASE) as file, patch.object(worker, 'free_bytes', return_value=worker.SNAPSHOT_RESERVE - 1):
                refused(file, None, 'Not enough free space')
            # A read that never ends (a stalled network or FUSE file) costs a bounded wait,
            # and so does a disk that fills up while copying.
            read, write = os.pipe()
            try:
                with tempfile.TemporaryFile(dir=instance) as snapshot, patch.object(worker.os, 'lseek'):
                    began = time.monotonic()
                    with self.assertRaisesRegex(RuntimeError, 'took too long.*Nothing was installed'):
                        worker.snapshot_package(read, snapshot, 100, instance, timeout=0.3, poll=0.05)
                    self.assertLess(time.monotonic() - began, 5)
                    space = iter([2 * worker.SNAPSHOT_RESERVE, 0])
                    with patch.object(worker, 'free_bytes', side_effect=lambda directory: next(space, 0)):
                        with self.assertRaisesRegex(RuntimeError, 'Not enough free space'):
                            worker.snapshot_package(read, snapshot, 100, instance, timeout=30, poll=0.05)
            finally: os.close(read); os.close(write)
            # The copy starts at the beginning wherever the caller left the position.
            with package(BASE, ARM) as file, tempfile.TemporaryFile(dir=instance) as snapshot:
                os.lseek(file.fileno(), 3, os.SEEK_SET)
                worker.snapshot_package(file.fileno(), snapshot, 9, instance)
                self.assertEqual(os.pread(snapshot.fileno(), 100, 0), (BASE + ARM)[:9])

    def test_commit_result_is_final_and_a_commit_timeout_is_never_abandoned(self):
        with tempfile.TemporaryDirectory() as directory, package(BASE, ARM) as file:
            sizes = [len(BASE), len(ARM)]
            fake = FakeAndroid(self, Path(directory), {'install-commit': RuntimeError('APK installation command timed out')})
            with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                with self.assertRaisesRegex(RuntimeError, 'may still be optimizing.*outcome is unknown'):
                    worker.install_apk(Path(directory), 'r-two', file.fileno(), parts=sizes)
            self.assertNotIn('install-abandon', [call[0] for call in fake.calls])
            for reply in ('Failure [INSTALL_FAILED_MISSING_SPLIT: Missing split for com.example]', 'Session staged'):
                fake = FakeAndroid(self, Path(directory), {'install-commit': reply})
                with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                    with self.assertRaisesRegex(RuntimeError, 'APK installation failed'):
                        worker.install_apk(Path(directory), 'r-two', file.fileno(), parts=sizes)
                self.assertNotIn('install-abandon', [call[0] for call in fake.calls])
            fake = FakeAndroid(self, Path(directory), {'install-commit': RuntimeError('APK installation failed: Failure [INSTALL_FAILED_NO_MATCHING_ABIS]')})
            with patch.object(worker.linux, 'run_bounded', side_effect=fake):
                with self.assertRaisesRegex(RuntimeError, 'NO_MATCHING_ABIS'):
                    worker.install_apk(Path(directory), 'r-two', file.fileno(), parts=sizes)
            self.assertNotIn('install-abandon', [call[0] for call in fake.calls])

    def test_desktop_session_shows_android_dialogs_only_for_the_commit(self):
        with tempfile.TemporaryDirectory() as directory, package(BASE, ARM) as file:
            fake = FakeAndroid(self, Path(directory))
            shown = []
            with patch.object(worker.linux, 'run_bounded', side_effect=fake), \
                 patch.object(worker, 'show_android_ui', side_effect=lambda *args: shown.append(len(fake.calls))):
                worker.install_apk(Path(directory), 'r-two', file.fileno(), desktop=True, parts=[len(BASE), len(ARM)])
            self.assertEqual(shown, [3])  # after create and both writes, before commit

    def test_single_apk_request_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, package(BASE) as file:
            def command(*args, **kwargs):
                self.assertEqual(list(map(str, args[7:])), ['install', '-r', '--user', '0', '-S', str(len(BASE))])
                return 'Success'
            with patch.object(worker.linux, 'run_bounded', side_effect=command) as run:
                self.assertTrue(worker.install_apk(Path(directory), 'r-two', file.fileno())['success'])
            self.assertEqual(run.call_count, 1)

    def test_backend_sends_sizes_only_to_a_worker_that_knows_sessions(self):
        backend = android.AndroidBackend(None)
        state = {'state': 'Running', 'generation': 'a', 'capabilities': ['install_apk']}
        with patch.object(backend, 'status', return_value=state) as status:
            with self.assertRaisesRegex(RuntimeError, 'Stop and start this device once'): backend.install('r-two', 7, [8, 13])
            self.assertEqual(status.call_count, 1)  # the file never reaches an old worker
        ready = dict(state, capabilities=['install_apk', 'install_splits'])
        done = dict(ready, app_result={'action': 'install', 'success': True})
        with patch.object(backend, 'status', side_effect=[ready, done]) as status:
            self.assertTrue(backend.install('r-two', 7, [8, 13])['success'])
            self.assertEqual(status.call_args.args, ('r-two', 'install', {'op': 'install', 'generation': 'a', 'parts': [8, 13]}))
            self.assertEqual(status.call_args.kwargs, {'apk_fd': 7})
        with patch.object(backend, 'status', side_effect=[state, dict(state, app_result={'action': 'install', 'success': True})]) as status:
            backend.install('r-two', 7)
            self.assertEqual(status.call_args.args[2], {'op': 'install', 'generation': 'a'})

    def test_a_worker_that_does_not_answer_in_time_leaves_the_outcome_unknown(self):
        import socket
        backend = android.AndroidBackend(None)
        state = {'state': 'Running', 'generation': 'a', 'capabilities': ['install_apk', 'install_splits']}
        # The wording the GUI shows as "Not confirmed", never as a plain failure.
        gui = (ROOT/'crates/anvildroid-gui/src-frontend/main.js').read_text()
        self.assertIn('/outcome is unknown|result is unknown|may still complete/i', gui)
        for failure in (socket.timeout('timed out'), ConnectionResetError('reset'), BrokenPipeError('pipe')):
            for parts in (None, [8, 13]):
                with self.subTest(failure=failure, parts=parts), patch.object(backend, 'status', side_effect=[state, failure]):
                    with self.assertRaisesRegex(RuntimeError, 'may still be installing.*outcome is unknown') as raised:
                        backend.install('r-two', 7, parts)
                    self.assertNotEqual(str(raised.exception), 'timed out')
        for changed in (dict(state, generation='b', app_result={'action': 'install', 'success': True}), dict(state, app_result={'action': 'other'})):
            with patch.object(backend, 'status', side_effect=[state, changed]):
                with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'): backend.install('r-two', 7)
        # A device that is not running was never handed the file: a plain refusal.
        with patch.object(backend, 'status', return_value=dict(state, state='Stopped')):
            with self.assertRaisesRegex(RuntimeError, '^Start this runtime'): backend.install('r-two', 7)


COMMITTING = 'mCommitted=true mSealed=true mDestroyed=false'
DOWNLOADING = 'mCommitted=false mSealed=false mDestroyed=false'


def sessions(*states, ready=True):
    """`pm path android` + `dumpsys package installs` as Android 13 prints them, cut to what is read."""
    text = 'package:/system/framework/framework-res.apk\n' if ready else ''
    text += 'Active install sessions:\n'
    for index, state in enumerate(states):
        text += (f'  Session {11 + index}:\n    userId=0 mOriginalInstallerUid=10123 installerPackageName=com.android.vending '
                 f'createdMillis=1763280000000 stageDir=/data/app/vmdl{11 + index}.tmp stageCid=null\n'
                 f'    mode=1 installFlags=0x400002\n    mClientProgress=1.0 mProgress=0.8 {state} mFds=0 mBridges=0\n\n')
    return text + '\nHistorical install sessions:\n  Session 7:\n    mCommitted=true mSealed=true mDestroyed=false\n\nLegacy install sessions:\n  {}\n'


RESTARTING = "cmd: Can't find service: package\nCan't find service: package"


class PackageWorkTests(unittest.TestCase):
    """Android's own installs: what counts as one, and who waits for it."""
    def test_android_13_headers_are_read_and_leftover_sessions_are_ignored(self):
        linux = worker.linux
        # Taken from Android 13 after a system process restart interrupted an install.
        dump = ('Active install sessions:\n'
                '  Active Session 1002949385:\n'
                '    userId=0 mOriginalInstallerUid=2000 mInstallerUid=2000 \n'
                '    createdMillis=1794828979946 updatedMillis=1794828979946 committedMillis=1794828980147 \n'
                '    mClientProgress=0.0 mProgress=0.0 mCommitted=true mSealed=true mPermissionsManuallyAccepted=false \n'
                '    mStageDirInUse=false mDestroyed=false mFds=0 mBridges=0 mFinalStatus=0 mFinalMessage=null \n'
                '  \n'
                'Finalized install sessions:\n\n'
                'Historical install sessions:\n'
                '  Session 896581799:\n'
                '      mClientProgress=1.0 mCommitted=true mSealed=true\n')
        committed, stale = 1794828980147, linux.PACKAGE_SESSION_STALE_MS
        self.assertEqual(linux.package_sessions(dump, committed + 5000), {'1002949385'})
        self.assertEqual(linux.package_sessions(dump, committed + stale - 1), {'1002949385'})
        self.assertEqual(linux.package_sessions(dump, committed + stale), set())
        self.assertEqual(linux.package_sessions(dump, committed + 19_000_000), set())
        # A clock that reads earlier than the commit never hides real work.
        self.assertEqual(linux.package_sessions(dump, committed - 60000), {'1002949385'})
        # Without a usable commit time the session still counts.
        text = 'Active install sessions:\n  Active Session 5:\n    committedMillis=0 mCommitted=true\n'
        self.assertEqual(linux.package_sessions(text, committed), {'5'})
        # The default clock is the present, long after this commit.
        self.assertEqual(linux.package_sessions(dump), set())

    def test_only_a_session_being_committed_counts(self):
        linux = worker.linux
        self.assertEqual(linux.package_sessions(sessions(COMMITTING)), {'11'})
        self.assertEqual(linux.package_sessions(sessions(DOWNLOADING, COMMITTING)), {'12'})
        self.assertEqual(linux.package_sessions(sessions(COMMITTING, COMMITTING)), {'11', '12'})
        # Android releases without mCommitted mark the same moment with mSealed.
        self.assertEqual(linux.package_sessions(sessions('mSealed=true mDestroyed=false')), {'11'})
        for idle in ((), (DOWNLOADING,), ('mCommitted=false mSealed=true mDestroyed=false',),
                     ('mCommitted=true mSealed=true mDestroyed=true',), ('mFds=0',)):
            self.assertEqual(linux.package_sessions(sessions(*idle)), set(), idle)
        # Finished sessions, other dumps and an Android that is restarting say nothing.
        for text in ('Historical install sessions:\n  Session 7:\n    mCommitted=true\n', 'Session 7:\n    mCommitted=true\n', RESTARTING, ''):
            self.assertEqual(linux.package_sessions(text), set(), text)
        self.assertTrue(linux.package_ready(sessions()))
        self.assertFalse(linux.package_ready(RESTARTING))
        self.assertFalse(linux.package_ready(sessions(ready=False)))

    def test_a_session_that_never_ends_stops_counting(self):
        hold = worker.linux.PackageHold()
        self.assertFalse(hold.busy(set(), 0))
        self.assertTrue(hold.busy({'11'}, 100))
        self.assertTrue(hold.busy({'11'}, 100 + hold.limit - 1))
        self.assertFalse(hold.busy({'11'}, 100 + hold.limit))
        # A new session starts its own allowance; a finished one is forgotten.
        self.assertTrue(hold.busy({'11', '12'}, 100 + hold.limit))
        self.assertFalse(hold.busy(set(), 100 + hold.limit + 1))
        self.assertTrue(hold.busy({'11'}, 100 + hold.limit + 2))

    def wait(self, replies, limit=60):
        """Run the worker's check against scripted answers; time moves only in pauses."""
        clock, probes = [1000.0], []
        def android(*args, **kwargs):
            args = list(map(str, args))
            self.assertEqual(args[:6] + args[7:], ['lxc-attach', '-P', '/runtime/android', '-n', 'r-two', '--', '-c', worker.linux.PACKAGE_PROBE])
            self.assertEqual(args[6], '/system/bin/sh')
            self.assertLessEqual(kwargs['timeout'], 8)
            probes.append(clock[0] - 1000)
            reply = replies[min(len(probes), len(replies)) - 1]
            if isinstance(reply, Exception): raise reply
            return reply
        with patch.object(worker.linux, 'run_bounded', side_effect=android), \
             patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            worker.await_package_service(Path('/runtime'), 'r-two', limit=limit,
                                         pause=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        return probes

    def test_install_goes_ahead_at_once_when_android_is_ready_and_idle(self):
        self.assertEqual(self.wait([sessions()]), [0])
        self.assertEqual(self.wait([sessions(DOWNLOADING)]), [0])

    def test_install_waits_for_an_install_in_flight_and_through_a_restart(self):
        # Busy, then the system process is gone, then no answer, then ready.
        replies = [sessions(COMMITTING), RESTARTING, RuntimeError('Package service check command timed out'), sessions()]
        self.assertEqual(self.wait(replies), [0, 2, 4, 6])

    def test_a_long_install_of_androids_own_delays_ours_only_for_the_limit(self):
        probes = self.wait([sessions(COMMITTING)], limit=10)
        self.assertEqual(probes, [0, 2, 4, 6, 8, 10])  # then the install is submitted anyway

    def test_an_android_that_stays_down_is_reported_as_restarting(self):
        for reply in (RESTARTING, RuntimeError('Package service check command timed out'), sessions(COMMITTING, ready=False)):
            with self.subTest(reply=reply), self.assertRaisesRegex(RuntimeError, 'package service is not ready.*Nothing was installed'):
                self.wait([reply], limit=6)

    probes, windows = 0, None

    def idle(self):
        state = {'state': 'Running'}
        idle = worker.IdleSuspend(Path('/runtime'), 'r-two', state, threading.Event())
        idle.last_action = 0
        return idle, state

    def tick(self, idle, state, now, packages):
        home = ('\nANVIL_IDLE_SECTION\ncom.android.launcher3/.Home\nANVIL_IDLE_SECTION\nRootTask id=1\n'
                ' taskId=1: com.android.launcher3/com.android.launcher3.Home visible=true\nANVIL_IDLE_SECTION\nACTIVITY MANAGER SERVICES\n')
        def lxc(tool, *args):
            if tool == 'lxc-attach' and args[-1] == worker.linux.PACKAGE_SESSIONS:
                # The session list is its own question, never part of the idle snapshot.
                self.probes += 1
                if isinstance(packages, Exception): raise packages
                return packages.strip()
            if tool == 'lxc-attach':
                self.assertNotIn('dumpsys package', args[-1])
                return (home if self.windows is None else self.windows).strip()
            if tool == 'lxc-info': return 'FROZEN' if state['state'] == 'Running' else 'RUNNING'
            return ''
        real = worker.IdleSuspend.lxc
        def bounded(tool, *args):
            # Each question to Android keeps the same small limits.
            with patch.object(worker.linux, 'run_bounded', side_effect=lambda *a, **k: (
                    self.assertEqual((k['timeout'], k['output_limit'], k['full_output']), (3, 1024 * 1024, True)), lxc(tool, *args))[1]):
                return real(idle, tool, *args)
        idle.lxc = Mock(side_effect=bounded)
        idle.next_check = 0
        with patch.object(worker.time, 'monotonic', return_value=now), patch.object(worker, 'list_apps', return_value=[]), \
             patch('builtins.print'):
            idle.tick()
        return state['state']

    def test_idle_device_is_not_frozen_while_android_installs_a_package(self):
        idle, state = self.idle()
        installs = sessions(COMMITTING).split('\n', 1)[1]
        self.assertEqual(self.tick(idle, state, 100, installs), 'Running')
        self.assertEqual(self.tick(idle, state, 300, installs), 'Running')   # far past the 60 s grace
        self.assertIsNone(idle.idle_since)
        # The grace period starts when the installation ends.
        done = sessions().split('\n', 1)[1]
        self.assertEqual(self.tick(idle, state, 310, done), 'Running')
        self.assertEqual(self.tick(idle, state, 369, done), 'Running')
        self.assertEqual(self.tick(idle, state, 371, done), 'Frozen')

    def test_idle_device_still_freezes_without_package_work_or_with_unknown_text(self):
        for packages in (sessions(DOWNLOADING).split('\n', 1)[1], '', 'unknown dump format', RESTARTING):
            with self.subTest(packages=packages):
                idle, state = self.idle()
                self.assertEqual(self.tick(idle, state, 100, packages), 'Running')
                self.assertEqual(self.tick(idle, state, 161, packages), 'Frozen')

    def test_a_stuck_session_holds_the_freeze_only_for_the_limit_also_across_resumes(self):
        idle, state = self.idle()
        stuck = sessions(COMMITTING).split('\n', 1)[1]
        limit = worker.linux.PackageHold.limit
        self.assertEqual(self.tick(idle, state, 100, stuck), 'Running')
        self.assertEqual(self.tick(idle, state, 99 + limit, stuck), 'Running')
        self.assertEqual(self.tick(idle, state, 100 + limit, stuck), 'Running')   # hold over; grace starts
        self.assertEqual(self.tick(idle, state, 161 + limit, stuck), 'Frozen')
        # Woken up again and again, the same session never earns a new hold:
        # only the usual grace period passes before the device sleeps again.
        for woke in (5000, 9000):
            with patch.object(worker.time, 'monotonic', return_value=woke), patch('builtins.print'):
                idle.resume()
            self.assertEqual(state['state'], 'Running')
            self.assertEqual(self.tick(idle, state, woke + 1, stuck), 'Running')
            self.assertEqual(idle.idle_since, woke + 1)
            self.assertEqual(self.tick(idle, state, woke + 62, stuck), 'Frozen')

    def test_time_asleep_does_not_use_up_the_allowance_of_a_session(self):
        hold = worker.linux.PackageHold()
        self.assertTrue(hold.busy({'5'}, 100))
        hold.slept(4000)                       # frozen from 200 to 4200
        self.assertTrue(hold.busy({'5'}, 4300))               # 200 s awake so far
        self.assertTrue(hold.busy({'5'}, 4100 + hold.limit - 1))
        self.assertFalse(hold.busy({'5'}, 4100 + hold.limit))
        self.assertTrue(hold.busy({'5', '6'}, 9000))          # a new session starts its own time

    def test_a_session_list_that_fails_or_is_too_long_never_keeps_the_device_awake(self):
        for failure in (RuntimeError('Android idle suspend command timed out'), RuntimeError('Android idle suspend output exceeds limit'),
                        subprocess.TimeoutExpired('lxc-attach', 3)):
            with self.subTest(failure=failure):
                idle, state = self.idle()
                self.assertEqual(self.tick(idle, state, 100, failure), 'Running')
                self.assertEqual(idle.idle_since, 100)       # counted as idle, not as an error
                self.assertEqual(self.tick(idle, state, 161, failure), 'Frozen')
        # A device with a window open is not asked about packages at all.
        idle, state = self.idle()
        self.windows, self.probes = 'unknown', 0
        try: self.assertEqual(self.tick(idle, state, 100, sessions(COMMITTING)), 'Running')
        finally: self.windows = None
        self.assertEqual(self.probes, 0)


class SystemFreezeTests(unittest.TestCase):
    """System Waydroid asks to be frozen through AnvilDroid's hook in its container service."""
    def setUp(self):
        self.system = load('system')
        self.integration = self.system.Integration.__new__(self.system.Integration)
        self.integration.original_freeze = Mock()
        self.integration.package_hold = self.system.existing.linux.PackageHold()
        self.status = Mock(return_value='RUNNING')
        self.integration.tools = SimpleNamespace(helpers=SimpleNamespace(lxc=SimpleNamespace(status=self.status)))
        lease = patch.object(self.system.Path, 'lstat', side_effect=FileNotFoundError())  # no ADB session
        lease.start(); self.addCleanup(lease.stop)

    def freeze(self, reply, now=100.0):
        def android(*args, **kwargs):
            self.assertEqual(list(map(str, args)), ['lxc-attach', '-P', '/var/lib/waydroid/lxc', '-n', 'waydroid', '--',
                                                    '/system/bin/sh', '-c', self.system.existing.linux.PACKAGE_SESSIONS])
            self.assertLessEqual(kwargs['timeout'], 3)
            if isinstance(reply, Exception): raise reply
            return reply
        self.integration.original_freeze.reset_mock()
        with patch.object(self.system.existing.linux, 'run_bounded', side_effect=android) as run, \
             patch.object(self.system.time, 'monotonic', return_value=now):
            self.integration.freeze('args')
        return self.integration.original_freeze.call_count == 1, run.call_count

    def test_freeze_is_skipped_only_while_android_commits_an_install(self):
        self.assertEqual(self.freeze(sessions(COMMITTING)), (False, 1))
        for reply in (sessions(), sessions(DOWNLOADING), '', 'unknown dump format', RESTARTING,
                      RuntimeError('Default package activity check command timed out'), OSError('lxc-attach missing')):
            with self.subTest(reply=reply):
                self.assertEqual(self.freeze(reply), (True, 1))
        self.integration.original_freeze.assert_called_once_with('args')

    def test_a_stuck_session_cannot_keep_system_waydroid_awake_for_good(self):
        limit = self.integration.package_hold.limit
        self.assertEqual(self.freeze(sessions(COMMITTING), now=100), (False, 1))
        self.assertEqual(self.freeze(sessions(COMMITTING), now=99 + limit), (False, 1))
        self.assertEqual(self.freeze(sessions(COMMITTING), now=100 + limit), (True, 1))

    def test_a_container_that_is_not_running_is_never_entered(self):
        for status in ('FROZEN', 'STOPPED'):
            self.status.return_value = status
            self.assertEqual(self.freeze(sessions(COMMITTING)), (True, 0))
        self.status.side_effect = RuntimeError('lxc-info failed')
        self.assertEqual(self.freeze(sessions(COMMITTING)), (True, 0))


@unittest.skipUnless(os.geteuid() == 0, 'Controller store ownership checks require root')
class ControllerSplitInstallTests(unittest.TestCase):
    def test_controller_validates_sizes_and_hands_its_own_descriptor_to_the_job(self):
        spec = importlib.util.spec_from_file_location('apk_install_controller', ROOT/'services/runtime-controller.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='anvil-apk-install-', dir='/tmp') as directory:
            root = Path(directory)
            (root/'store').mkdir(mode=0o700)
            (root/'images').mkdir(mode=0o700)
            store = module.Store(root/'store', root/'images')
            gate = threading.Event()
            seen = []
            try:
                record = store.dispatch({'op': 'create', 'name': 'Split target'}, 65534)
                identifier = record['id']
                store.replace(dict(record, state='Running', job={'id': 'a' * 32, 'operation': 'start', 'status': 'Succeeded', 'error': None}))
                request = {'op': 'install', 'id': identifier, 'parts': list(SIZES)}
                with package(BASE, ARM, DPI) as file:
                    fd = file.fileno()
                    for bad in ([sum(SIZES)], [8, 13], [9, 12, 10], 'x', None, [8, 13, '10']):
                        with self.subTest(parts=bad), self.assertRaises(module.RequestError) as refused:
                            store.dispatch(dict(request, parts=bad), 65534, fd)
                        self.assertEqual(refused.exception.code, 'INVALID_APK')
                    for extra in ({'path': '/etc/shadow'}, {'uid': 0}, {'names': ['a']}):
                        with self.assertRaises(module.RequestError) as refused:
                            store.dispatch(dict(request, **extra), 65534, fd)
                        self.assertEqual(refused.exception.code, 'INVALID_REQUEST')
                    with self.assertRaises(module.RequestError): store.dispatch(request, 0, fd)       # not the owner
                    with self.assertRaises(module.RequestError): store.dispatch(request, 65534)       # no descriptor
                    with self.assertRaises(module.RequestError): store.dispatch(dict(request, id='default'), 65534, fd)
                    self.assertEqual(store.active_jobs, set())
                    # The caller's file is read before the lock every request waits for is taken.
                    def lock_is_free():
                        outcome = []
                        def probe():
                            taken = store.mutex.acquire(timeout=2)
                            if taken: store.mutex.release()
                            outcome.append(taken)
                        thread = threading.Thread(target=probe); thread.start(); thread.join()
                        return outcome[0]
                    reads = []
                    for name in ('apk_parts', 'apk_size'):
                        real = getattr(module.transfer, name)
                        def checked(*args, real=real, name=name):
                            reads.append((name, lock_is_free()))
                            return real(*args)
                        patcher = patch.object(module.transfer, name, side_effect=checked); patcher.start(); self.addCleanup(patcher.stop)
                    with patch.object(module.transfer, 'part_sizes', side_effect=lambda parts: (_ for _ in ()).throw(ValueError('held'))):
                        with self.assertRaises(module.RequestError): store.dispatch(request, 65534, fd)
                    with self.assertRaises(module.RequestError): store.dispatch({'op': 'install', 'id': identifier, 'path': 'x'}, 65534, fd)
                    self.assertEqual(reads, [('apk_parts', True), ('apk_size', True)])
                    def install(target, job_fd, parts):
                        gate.wait(5)
                        seen.append((target, parts, os.pread(job_fd, sum(parts), 0), job_fd != fd))
                        return {'action': 'install', 'success': True}
                    with patch.object(store.android, 'install', side_effect=install):
                        accepted = store.dispatch(request, 65534, fd)
                        self.assertEqual((accepted['action'], accepted['status']), ('install', 'Running'))
                        with self.assertRaises(module.RequestError): store.dispatch(request, 65534, fd)  # one job per device
                        file.close()  # The job owns its duplicated descriptor.
                        gate.set(); store.pool.shutdown(wait=True)
                self.assertEqual(seen, [(identifier, SIZES, BASE + ARM + DPI, True)])
                job = store.dispatch({'op': 'app_job', 'id': identifier}, 65534)
                self.assertEqual((job['id'], job['status']), (accepted['id'], 'Succeeded'))
                self.assertNotIn(identifier, store.active_jobs)
            finally:
                gate.set(); store.close()


if __name__ == '__main__': unittest.main()
