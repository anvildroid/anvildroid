import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'native/anvildroid-apps.sh'


class CompanionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anvil-app-companion-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bindir = self.root / 'bin'
        bindir.mkdir()
        for name in ('am', 'dumpsys'):
            command = bindir / name
            command.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$TRACE"\necho Success\n')
            command.chmod(0o755)
        # Every pm step must be bounded. This stand-in records the limit and can
        # play a step that ran out of time (exit 124, as timeout reports it).
        limit = bindir / 'timeout'
        limit.write_text('#!/bin/sh\nprintf "timeout %s\\n" "$*" >> "$TRACE"\n'
                         'case "$(cat "$FIXTURE/mode") $*" in "commit-timeout "*install-commit*|"install-timeout "*" pm install -r"*) exit 124;; esac\n'
                         'shift\nexec "$@"\n')
        limit.chmod(0o755)
        pm = bindir / 'pm'
        pm.write_text('''#!/usr/bin/python3
import os, sys
from pathlib import Path
root = Path(os.environ['FIXTURE'])
args = sys.argv[1:]
with (root/'trace').open('a') as trace: trace.write('pm ' + ' '.join(args) + '\\n')
mode = (root/'mode').read_text().strip()
if args[:2] == ['list', 'packages']:
    if mode == 'query-fail': sys.exit(1)
    if '-3' not in args or mode != 'system':
        if (root/'installed').exists():
            print('package:com.example.test' + ('.other' if mode == 'similar' else ''))
elif args[:1] == ['uninstall']:
    if mode == 'failure': print('Failure [DELETE_FAILED_INTERNAL_ERROR]')
    else:
        if mode != 'sticky': (root/'installed').unlink(missing_ok=True)
        print('Success')
elif args[:1] == ['install']: print('Success')
elif args[:1] == ['install-create']:
    if mode == 'no-session': print('Error: no session'); sys.exit(1)
    print('Success: created install session [41]')
elif args[:1] == ['install-write']:
    data = sys.stdin.buffer.read()
    if mode == 'write-fail' and args[4] == '1.apk': print('Error: bad split'); sys.exit(1)
    with (root/'written').open('a') as written: written.write(' '.join(args[2:]) + ' ' + data.decode() + '\\n')
    print('Success: streamed %d bytes' % len(data))
elif args[:1] == ['install-commit']:
    if mode == 'commit-fail': print('Failure [INSTALL_FAILED_MISSING_SPLIT]'); sys.exit(1)
    print('Success')
elif args[:1] == ['install-abandon']: print('Success')
elif args == ['path', 'android']:
    if mode == 'restarting': print("cmd: Can't find service: package"); sys.exit(20)
    print('package:/system/framework/framework-res.apk')
else: sys.exit(2)
''')
        pm.chmod(0o755)
        (self.root/'mode').write_text('normal')
        (self.root/'installed').touch()
        script = SCRIPT.read_text().replace('base=/data/waydroid_tmp/anvildroid', 'base=' + str(self.root))
        self.process = subprocess.Popen(['/bin/sh'], stdin=subprocess.PIPE,
            env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                 'TRACE': str(self.root/'trace'), 'FIXTURE': str(self.root)})
        self.process.stdin.write(script.encode())
        self.process.stdin.close()
        self.addCleanup(self.stop)
        for _ in range(100):
            if (self.root/'apps').is_dir(): break
            time.sleep(.01)
        self.counter = 0

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=3)

    def job(self, action, package, files=None):
        self.counter += 1
        name = 'job-' + str(self.counter)
        temporary = self.root/'apps'/(name+'.staging')
        temporary.mkdir()
        (temporary/'action').write_text(action+'\n')
        (temporary/'package').write_text(package+'\n')
        for file, content in (files or {'input.apk': b'fixture'}).items():
            if content is None: os.mkfifo(temporary/file)
            elif isinstance(content, str): (temporary/file).symlink_to(content)
            else: (temporary/file).write_bytes(content)
        temporary.rename(self.root/'apps'/(name+'.ready'))
        done = self.root/'apps'/(name+'.working')
        for _ in range(200):
            if (done/'status').exists():
                return ((done/'status').read_text().strip(), (done/'output').read_text())
            time.sleep(.01)
        self.fail('companion timeout')

    def trace(self):
        path = self.root/'trace'
        return path.read_text() if path.exists() else ''

    def test_queue_validation_and_protected_components(self):
        for action, package in [('force-stop', 'com.android.settings'),
                                ('uninstall', 'cu.axel.smartdock'),
                                ('uninstall', 'org.anvildroid.ime'),
                                ('details', 'com.test;id'), ('shell', 'com.example.test')]:
            self.assertEqual(self.job(action, package)[0], '2')
        self.assertEqual(self.trace(), '')
        for action in ('install', 'force-stop', 'details'):
            self.assertEqual(self.job(action, 'com.example.test')[0], '0')
        self.assertIn('install -r --user 0', self.trace())
        self.assertIn('force-stop --user 0 com.example.test', self.trace())

    def test_installs_reports_the_package_service_and_its_sessions(self):
        status, output = self.job('installs', 'android', {})
        self.assertEqual((status, output.splitlines()), ('0', ['package:/system/framework/framework-res.apk', 'Success']))
        self.assertEqual(self.trace().splitlines()[0], 'pm path android')
        self.assertTrue(self.trace().splitlines()[1].endswith('dumpsys package installs'))
        # While Android restarts the answer still comes back for the host to read.
        (self.root/'mode').write_text('restarting')
        status, output = self.job('installs', 'android', {})
        self.assertEqual(status, '0')
        self.assertIn("Can't find service: package", output)
        self.assertFalse(any(line.startswith('package:') for line in output.splitlines()))

    def test_split_package_is_one_session_with_the_base_first(self):
        splits = {'count': b'3\n', 'input-0.apk': b'base', 'input-1.apk': b'arm64', 'input-2.apk': b'xxhdpi',
                  'evil;name.apk': b'not one of the numbered files'}
        status, output = self.job('install', 'com.example.test', splits)
        self.assertEqual((status, output.strip()), ('0', 'Success'))
        self.assertEqual((self.root/'written').read_text(),
                         '4 41 0.apk base\n5 41 1.apk arm64\n6 41 2.apk xxhdpi\n')
        trace = self.trace()
        self.assertIn('pm install-create -r --user 0\n', trace)
        self.assertTrue(trace.endswith('pm install-commit 41\n'))
        self.assertNotIn('pm install -r', trace)
        self.assertNotIn('abandon', trace)
        self.assertNotIn('evil', trace)
        # Every step that talks to Android has a time limit.
        steps = [line for line in trace.splitlines() if line.startswith('timeout ')]
        self.assertEqual([step.split()[1] for step in steps], ['30', '150', '150', '150', '900'])
        self.assertEqual(len([line for line in trace.splitlines() if line.startswith('pm ')]), 5)

    def test_a_split_set_that_is_not_exactly_what_the_host_staged_installs_nothing(self):
        base = {'input-0.apk': b'base', 'input-1.apk': b'arm64', 'input-2.apk': b'xxhdpi'}
        target = self.root/'elsewhere.apk'
        target.write_bytes(b'not staged by the host')
        cases = {
            'a gap': {'count': b'3\n', 'input-0.apk': b'base', 'input-1.apk': b'arm64', 'input-3.apk': b'late'},
            'a missing file': {'count': b'3\n', 'input-0.apk': b'base', 'input-1.apk': b'arm64'},
            'an extra file': dict(base, count=b'2\n'),
            'an extra single APK': dict(base, **{'count': b'3\n', 'input.apk': b'single'}),
            'a link in the middle': dict(base, **{'count': b'3\n', 'input-1.apk': str(target)}),
            'a linked base': dict(base, **{'count': b'3\n', 'input-0.apk': str(target)}),
            'a dangling link beyond the set': dict(base, **{'count': b'3\n', 'input-7.apk': '/nonexistent'}),
            'a pipe': dict(base, **{'count': b'3\n', 'input-2.apk': None}),
            'no count': base,
            'a linked count': dict(base, count=str(target)),
            'a count of one': {'count': b'1\n', 'input-0.apk': b'base'},
            'a count of zero': {'count': b'0\n'},
            'a count that is not a number': dict(base, count=b'3; id\n'),
            'a count that is too large': dict(base, count=b'513\n'),
            'a count with a leading zero': dict(base, count=b'03\n'),
        }
        for name, files in cases.items():
            with self.subTest(name):
                status, output = self.job('install', 'com.example.test', files)
                self.assertEqual(status, '2', output)
                self.assertTrue(output.strip(), 'the refusal must say why')
                self.assertNotIn('Success', output)
                self.assertEqual(self.trace(), '')  # Android was never asked
                self.assertFalse((self.root/'written').exists())

    def test_install_steps_that_run_out_of_time_are_reported_as_unconfirmed(self):
        for mode, files in (('commit-timeout', {'count': b'2\n', 'input-0.apk': b'base', 'input-1.apk': b'arm64'}),
                            ('install-timeout', None)):
            with self.subTest(mode):
                (self.root/'mode').write_text(mode)
                status, output = self.job('install', 'com.example.test', files)
                self.assertEqual(status, '1')
                self.assertIn('may still complete', output)
                self.assertNotIn('Success', output.splitlines())
                self.assertNotIn('abandon', self.trace())  # a commit may still finish
        (self.root/'mode').write_text('normal')
        (self.root/'trace').unlink()
        self.assertEqual(self.job('install', 'com.example.test')[0], '0')
        self.assertIn('timeout 900 pm install -r --user 0', self.trace())

    def test_split_failures_abandon_before_commit_and_never_after(self):
        splits = {'count': b'2\n', 'input-0.apk': b'base', 'input-1.apk': b'arm64'}
        (self.root/'mode').write_text('write-fail')
        status, output = self.job('install', 'com.example.test', splits)
        self.assertEqual(status, '1')
        self.assertIn('bad split', output)
        self.assertIn('nothing was installed', output)
        self.assertNotIn('Success', output.splitlines())
        self.assertIn('pm install-abandon 41\n', self.trace())
        self.assertNotIn('install-commit', self.trace())
        (self.root/'trace').unlink()
        (self.root/'mode').write_text('commit-fail')
        status, output = self.job('install', 'com.example.test', splits)
        self.assertEqual(status, '1')
        self.assertIn('INSTALL_FAILED_MISSING_SPLIT', output)
        self.assertNotIn('abandon', self.trace())
        (self.root/'trace').unlink()
        (self.root/'mode').write_text('no-session')
        status, output = self.job('install', 'com.example.test', splits)
        self.assertEqual(status, '1')
        self.assertIn('did not open an install session', output)
        self.assertNotIn('install-write', self.trace())
        # A link is never followed, and a request without an APK says so.
        (self.root/'mode').write_text('normal')
        (self.root/'trace').unlink()
        self.counter += 1
        staging = self.root/'apps'/'job-link.staging'
        staging.mkdir()
        (staging/'action').write_text('install\n')
        (staging/'package').write_text('com.example.test\n')
        (staging/'input.apk').symlink_to('/etc/hostname')
        staging.rename(self.root/'apps'/'job-link.ready')
        done = self.root/'apps'/'job-link.working'
        for _ in range(200):
            if (done/'status').exists(): break
            time.sleep(.01)
        self.assertEqual((done/'status').read_text().strip(), '2')
        self.assertEqual(self.trace(), '')

    def test_uninstall_confirms_third_party_and_actual_removal(self):
        status, output = self.job('uninstall', 'com.example.test')
        self.assertEqual((status, output.strip()), ('0', 'Success'))
        self.assertFalse((self.root/'installed').exists())
        self.assertIn('pm uninstall --user 0 com.example.test\n', self.trace())

    def test_system_and_partial_package_matches_never_uninstall(self):
        for mode in ('system', 'similar', 'query-fail'):
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode)
                self.assertNotEqual(self.job('uninstall', 'com.example.test')[0], '0')
                self.assertNotIn('pm uninstall', self.trace())
                self.assertTrue((self.root/'installed').exists())

    def test_android_failure_or_retained_package_cannot_report_success(self):
        for mode, message in [('failure', 'DELETE_FAILED_INTERNAL_ERROR'),
                              ('sticky', 'still reports the app installed')]:
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode)
                status, output = self.job('uninstall', 'com.example.test')
                self.assertNotEqual(status, '0')
                self.assertIn(message, output)
                self.assertTrue((self.root/'installed').exists())


if __name__ == '__main__':
    unittest.main()
