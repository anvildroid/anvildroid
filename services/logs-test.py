#!/usr/bin/env python3
"""Inert tests for the device log reader (runtime-logs.py). Samples come from a real Android 13 device."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('runtime_logs', Path(__file__).with_name('runtime-logs.py'))
logs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logs)

# logcat -d -v epoch,printable (with -T, --pid and -b crash), copied from the device.
BOOT = b'''--------- beginning of main
         1795077089.892  7099  7138 D BootAnimation: Playing files = /product/media/bootanimation.zip/part1, Requested repeat = 0, playUntilComplete = true
         1795077091.903  7099  7138 D BootAnimation: Playing files = /product/media/bootanimation.zip/part1, Requested repeat = 0, playUntilComplete = true
'''
SINCE = b'''--------- beginning of system
         1795076657.003   361   598 D CompatibilityChangeReporter: Compat change id reported: 135634846; UID 99000; state: DISABLED
         1795076657.003   361   406 D CompatibilityChangeReporter: Compat change id reported: 143937733; UID 10094; state: ENABLED
--------- beginning of main
'''
TRACE = b'''--------- beginning of main
         1795077053.046   361   760 W FastPrintWriter: Write failure
         1795077053.046   361   760 W FastPrintWriter: java.io.IOException: write failed: EPIPE (Broken pipe)
         1795077053.046   361   760 W FastPrintWriter: \tat libcore.io.IoBridge.write(IoBridge.java:651)
         1795077053.046   361   760 W FastPrintWriter: \tat java.io.FileOutputStream.write(FileOutputStream.java:401)
         1795077053.046   361   760 W FastPrintWriter: \t... 9 more
         1795077058.189   361   760 W FastPrintWriter: Write failure
         1795077058.189   361   760 W FastPrintWriter: java.io.IOException: write failed: EPIPE (Broken pipe)
         1795077058.189   361   760 W FastPrintWriter: \tat libcore.io.IoBridge.write(IoBridge.java:651)
'''
CRASH = b'''--------- beginning of crash
         1795076666.305    98  1408 F DEBUG   :       #16 pc 0000000000013e55  /system/lib64/libutils.so (android::Thread::_threadLoop(void*)+325) (BuildId: 056174139c244cde0be9a8d5970636f2)
         1795076666.305    98  1408 F DEBUG   :       #17 pc 00000000000ccd0a  /apex/com.android.runtime/lib64/bionic/libc.so (__pthread_start(void*)+58) (BuildId: edac77664d4699371f98b0f76841bb37)
'''
# ps -A -o PID,NAME on the same device, plus a second process of one app.
PS = b'''    PID NAME
      1 init
    361 system_server
    648 com.android.systemui
    680 com.google.android.gms
    901 com.android.networkstack.process
   1016 com.android.launcher3
   1051 com.android.smspush
   2040 com.google.android.gms:persistent
   2041 com.google.android.gms.unstable
'''
ID = 'r-' + '1' * 32


def params(**extra):
    return {'lines': 500, 'level': 'V', 'package': None, 'since': None, **extra}


def line(seconds, text, pid=100, level='I', tag='Tag', tid=None):
    return f'         {seconds}  {pid:>4}  {tid or pid:>4} {level} {tag}: {text}\n'.encode()


class Device:
    """A stand-in for run(): answers `ps` and `logcat` from fixed bytes and records every argv."""
    def __init__(self, logcat=b'', ps=PS):
        self.logcat, self.ps, self.calls = logcat, ps, []

    def __call__(self, tool, *args, timeout=8, limit=256 * 1024, sink=None, tail=False, check=True):
        self.calls.append((tool, args, {'timeout': timeout, 'limit': limit, 'tail': tail}))
        output = {'ps': self.ps, 'logcat': self.logcat}[tool]
        output = output(args) if callable(output) else output
        return output[-limit:] if tail else output

    def argv(self, tool='logcat'):
        return [args for name, args, _ in self.calls if name == tool]


def texts(report):
    return [item['message'] for item in report['lines']]


class Parsing(unittest.TestCase):
    def test_real_lines_become_structured_and_separators_are_dropped(self):
        device = Device(BOOT + SINCE + CRASH)
        report = logs.logs(device, params(), None)
        self.assertEqual(len(report['lines']), 6)
        self.assertEqual(report['lines'][0], {
            'time': 1795077089892, 'pid': 7099, 'tid': 7138, 'level': 'D', 'tag': 'BootAnimation',
            # The shared redaction treats "/media/..." as a possible user folder, also inside this system path.
            'message': 'Playing files = /product<user-path> Requested repeat = 0, playUntilComplete = true'})
        self.assertEqual(report['lines'][2]['tag'], 'CompatibilityChangeReporter')
        self.assertEqual(report['lines'][2]['message'], 'Compat change id reported: 135634846; UID 99000; state: DISABLED')
        self.assertEqual([report['lines'][3]['pid'], report['lines'][3]['tid']], [361, 406])
        # The crash buffer pads the tag; fatal lines keep their level.
        self.assertEqual([report['lines'][4]['level'], report['lines'][4]['tag']], ['F', 'DEBUG'])
        self.assertTrue(report['lines'][4]['message'].startswith('      #16 pc 0000000000013e55  /system/lib64/libutils.so'))
        self.assertEqual([report['unparsed'], report['truncated'], report['note']], [0, False, None])
        json.dumps(report, allow_nan=False)

    def test_fixed_argv_reads_main_system_and_crash_and_filters_by_level(self):
        device = Device(TRACE)
        logs.logs(device, params(level='W'), None)
        self.assertEqual(device.calls, [('logcat', ('-d', '-b', 'main,system,crash', '-v', 'epoch,printable', '*:W'),
                                         {'timeout': 8, 'limit': logs.READ_LIMIT, 'tail': True})])
        for level in logs.LEVELS:
            device = Device(TRACE)
            logs.logs(device, params(level=level), None)
            self.assertEqual(device.argv()[0][-1], '*:' + level)

    def test_stack_trace_lines_keep_their_tab_and_order(self):
        report = logs.logs(Device(TRACE), params(), None)
        self.assertEqual(texts(report)[:3], ['Write failure', 'java.io.IOException: write failed: EPIPE (Broken pipe)',
                                             '\tat libcore.io.IoBridge.write(IoBridge.java:651)'])
        self.assertEqual(len(report['lines']), 8)

    def test_odd_lines_are_counted_never_guessed(self):
        raw = (b'logcat: something unexpected\n' + line('1795077000.100', 'kept') + b'\r\n\n'
               + b'         1795077000.200   100   100 I NoMessage:\n'
               + b'         1795077000.300   100   100 I a:b: colon in tag\n'
               + b'         1795077000.400123   100   100 E Micro: usec format\n'
               + b'11-19 15:31:29.892  7099  7138 D BootAnimation: threadtime format\n')
        report = logs.logs(Device(raw), params(), None)
        self.assertEqual(report['unparsed'], 2)
        self.assertEqual([(item['tag'], item['message']) for item in report['lines']],
                         [('Tag', 'kept'), ('NoMessage', ''), ('a:b', 'colon in tag'), ('Micro', 'usec format')])
        self.assertEqual(report['lines'][-1]['time'], 1795077000400)

    def test_text_is_valid_json_without_control_characters(self):
        raw = (line('1795077000.100', 'bad \xff byte').replace(b'\xc3\xbf', b'\xff')
               + line('1795077000.200', 'bell\x07 esc\x1b[31mred\x1b[0m tab\there nul\x00 emoji \U0001f600'))
        report = logs.logs(Device(raw), params(), None)
        self.assertEqual(texts(report), ['bad � byte', 'bell escred tab\there nul emoji \U0001f600'])
        self.assertEqual(json.loads(json.dumps(report))['lines'], report['lines'])


class Redaction(unittest.TestCase):
    def test_recognised_secrets_are_hidden_like_in_diagnostic_reports(self):
        raw = (line('1795077000.100', 'GET https://user:hunter2@example.com/a?token=abc123&x=1')
               + line('1795077000.200', 'Authorization: Bearer abcdef0123456789')
               + line('1795077000.300', 'login password=topsecret for someone@example.com from 192.168.1.20')
               + line('1795077000.400', 'payload {\\"api_key\\":\\"sk-live-1\\"}')
               + line('1795077000.500', 'plain line stays'))
        report = logs.logs(Device(raw), params(), None)
        joined = '\n'.join(texts(report))
        for secret in ('hunter2', 'abc123', 'abcdef0123456789', 'topsecret', 'someone@example.com', '192.168.1.20', 'sk-live-1'):
            self.assertNotIn(secret, joined)
        self.assertIn('<redacted>', joined)
        self.assertEqual(texts(report)[-1], 'plain line stays')
        self.assertEqual(logs.diagnostics.redact_text.__module__, 'runtime_diagnostics')

    def test_an_escaped_line_break_cannot_split_a_line(self):
        report = logs.logs(Device(line('1795077000.100', 'first\\nsecond\\r\\nthird')), params(), None)
        self.assertEqual(texts(report), ['first second third'])


class Hostile(unittest.TestCase):
    def test_redaction_cost_is_bounded_for_text_an_app_chooses(self):
        import time
        nasty = ('a-' * 2000 + 'a').encode()
        raw = b''.join(b'         1795077089.%03d   100   100 I Tag: ' % (index % 1000) + nasty + b'\n' for index in range(150))
        started = time.monotonic()
        report = logs.logs(Device(raw), params(lines=2000), None)
        self.assertLess(time.monotonic() - started, logs.CLEAN_BUDGET + 3)
        self.assertTrue(report['lines'])
        self.assertTrue(all(len(item['message']) <= logs.MAX_MESSAGE + 2 for item in report['lines']))

    def test_redaction_stops_at_its_time_budget_and_says_so(self):
        raw = b''.join(line(f'1795077089.{index:03d}', f'message {index}') for index in range(200))
        with mock.patch.object(logs, 'CLEAN_BUDGET', -1):
            report = logs.logs(Device(raw), params(), None)
        self.assertEqual(report['lines'], [])
        self.assertTrue(report['truncated'])

    def test_a_line_from_the_future_cannot_become_the_position(self):
        raw = line('1795077089.100', 'real') + line('4102444800.000', 'forged') + line('1795077089.200', 'real too')
        with mock.patch.object(logs, 'clock', lambda: 1795077090 * 10**9):
            report = logs.logs(Device(raw), params(), None)
        self.assertEqual(texts(report), ['real', 'real too'])
        self.assertEqual(report['unparsed'], 1)
        self.assertTrue(report['cursor'].startswith('1795077089200000000:'))


class Limits(unittest.TestCase):
    def test_line_count_keeps_the_newest_and_reports_truncation(self):
        raw = b''.join(line(f'1795077{index:03d}.000', f'message {index}') for index in range(120))
        report = logs.logs(Device(raw), params(lines=50), None)
        self.assertEqual(texts(report), [f'message {index}' for index in range(70, 120)])
        self.assertTrue(report['truncated'])
        self.assertFalse(logs.logs(Device(raw), params(lines=120), None)['truncated'])

    def test_long_lines_are_cut_and_the_result_stays_small(self):
        report = logs.logs(Device(line('1795077000.100', 'x' * 5000, tag='T' * 300)), params(), None)
        self.assertEqual(len(report['lines'][0]['message']), logs.MAX_MESSAGE + 2)
        self.assertTrue(report['lines'][0]['message'].endswith(' …'))
        self.assertEqual(len(report['lines'][0]['tag']), logs.MAX_TAG + 2)
        # Non-ASCII text grows six times in ASCII JSON: the byte budget counts the encoded size.
        raw = b''.join(line(f'1795077{index:03d}.000', 'é' * 1500 + str(index)) for index in range(200))
        self.assertLess(len(raw), logs.READ_LIMIT)
        report = logs.logs(Device(raw), params(lines=2000), None)
        self.assertTrue(report['truncated'])
        self.assertLess(len(json.dumps(report)), 600 * 1024)
        self.assertTrue(0 < len(report['lines']) < 200)
        self.assertTrue(report['lines'][-1]['message'].endswith('199'))

    def test_a_bounded_read_drops_the_partial_first_line(self):
        raw = b''.join(line(f'17950{index:05d}.000', 'y' * 200 + f' end{index}') for index in range(4000))
        self.assertGreater(len(raw), logs.READ_LIMIT)
        device = Device(raw)
        report = logs.logs(device, params(lines=2000), None)
        self.assertEqual([report['unparsed'], report['truncated']], [0, True])
        self.assertTrue(all(text.startswith('y' * 200 + ' end') for text in texts(report)))
        self.assertTrue(texts(report)[-1].endswith(' end3999'))
        # The cursor follows the newest line that was read, so following continues after it.
        self.assertTrue(report['cursor'].startswith('1795003999000000000:'))


class Cursor(unittest.TestCase):
    def follow(self, device, cursor, **extra):
        report = logs.logs(device, params(since=cursor, **extra), None)
        return report, texts(report)

    def test_no_line_is_repeated_or_lost_inside_one_timestamp(self):
        first = TRACE
        report, shown = self.follow(Device(first), None)
        self.assertEqual(len(shown), 8)
        moment, hashes = report['cursor'].split(':')
        self.assertEqual([moment, len(hashes.split(','))], ['1795077058189000000', 3])
        # Nothing new: the same dump again gives no line and the same cursor.
        device = Device(first)
        again, shown = self.follow(device, report['cursor'])
        self.assertEqual([shown, again['cursor'], again['truncated']], [[], report['cursor'], False])
        # logcat is asked from one millisecond earlier, whatever `-T` does with an equal timestamp.
        self.assertEqual(device.argv()[0], ('-d', '-b', 'main,system,crash', '-v', 'epoch,printable', '-T', '1795077058.188', '*:V'))
        # More lines arrive with the SAME timestamp, then a later one.
        more = (b'         1795077058.189   361   760 W FastPrintWriter: \tat java.io.FileOutputStream.write(FileOutputStream.java:401)\n'
                b'         1795077058.189   361   760 W FastPrintWriter: \t... 9 more\n'
                b'         1795077060.000   361   760 W FastPrintWriter: Write failure\n')
        later, shown = self.follow(Device(first + more), report['cursor'])
        self.assertEqual(shown, ['\tat java.io.FileOutputStream.write(FileOutputStream.java:401)', '\t... 9 more', 'Write failure'])
        self.assertTrue(later['cursor'].startswith('1795077060000000000:'))
        # `-T` that starts after the cursor's instant (exclusive) or one line early loses and repeats nothing either.
        self.assertEqual(self.follow(Device(more), report['cursor'])[1], shown)
        early = b'         1795077058.188   361   760 W FastPrintWriter: older\n'
        self.assertEqual(self.follow(Device(early + first[first.index(b'         1795077058'):] + more), report['cursor'])[1], shown)

    def test_identical_lines_in_one_millisecond_are_counted(self):
        same = line('1795077000.500', 'tick')
        report, shown = self.follow(Device(same * 2), None)
        self.assertEqual(shown, ['tick', 'tick'])
        self.assertEqual(self.follow(Device(same * 2), report['cursor'])[1], [])
        self.assertEqual(self.follow(Device(same * 5), report['cursor'])[1], ['tick'] * 3)

    def test_cursor_advances_past_lines_that_the_filters_hide(self):
        raw = line('1795077000.100', 'launcher', pid=1016) + line('1795077000.900', 'other app', pid=648)
        report, shown = self.follow(Device(raw), None, package='com.android.launcher3')
        self.assertEqual(shown, ['launcher'])
        self.assertTrue(report['cursor'].startswith('1795077000900000000:'))

    def test_an_empty_or_cleared_buffer_keeps_the_cursor(self):
        cursor = logs.logs(Device(BOOT), params(), None)['cursor']
        self.assertEqual(logs.logs(Device(b''), params(since=cursor), None)['cursor'], cursor)
        self.assertEqual(logs.logs(Device(b'--------- beginning of main\n'), params(since=cursor), None)['cursor'], cursor)
        self.assertIsNone(logs.logs(Device(b''), params(), None)['cursor'])

    def test_many_lines_in_the_last_millisecond_fit_in_the_cursor(self):
        raw = b''.join(line('1795077000.500', f'burst {index}') for index in range(600))
        report, shown = self.follow(Device(raw), None, lines=2000)
        self.assertEqual(len(shown), 600)
        self.assertLessEqual(len(report['cursor']), 21 + 9 * logs.MAX_CURSOR_HASHES)
        logs.parse_cursor(report['cursor'])
        # Lines beyond the cursor's capacity may come again. They are never lost.
        again = self.follow(Device(raw + line('1795077000.500', 'new')), report['cursor'], lines=2000)[1]
        self.assertEqual(again[-1], 'new')
        self.assertEqual(len(again), 600 - logs.MAX_CURSOR_HASHES + 1)

    def test_start_argument_never_goes_below_zero(self):
        self.assertEqual(logs.start_argument(1795077058189000000), '1795077058.188')
        self.assertEqual(logs.start_argument(1795077058000999999), '1795077057.999')
        self.assertEqual(logs.start_argument(5), '0.000')


class Package(unittest.TestCase):
    def test_one_process_uses_pid_and_keeps_only_its_lines(self):
        raw = line('1795077000.100', 'mine', pid=1016) + line('1795077000.200', 'stale pid reuse', pid=7)
        device = Device(raw)
        report = logs.logs(device, params(package='com.android.launcher3'), None)
        self.assertEqual(device.argv('ps'), [('-A', '-o', 'PID,NAME')])
        self.assertEqual(device.argv()[0], ('-d', '-b', 'main,system,crash', '-v', 'epoch,printable', '--pid=1016', '*:V'))
        self.assertEqual(texts(report), ['mine'])

    def test_every_process_of_an_app_is_included_without_pid(self):
        raw = (line('1795077000.100', 'main process', pid=680) + line('1795077000.200', 'system', pid=361)
               + line('1795077000.300', 'second process', pid=2040) + line('1795077000.400', 'other package', pid=2041))
        device = Device(raw)
        report = logs.logs(device, params(package='com.google.android.gms'), None)
        self.assertFalse(any(arg.startswith('--pid') for arg in device.argv()[0]))
        self.assertEqual(texts(report), ['main process', 'second process'])

    def test_an_app_that_is_not_running_is_a_note_not_an_error(self):
        device = Device(BOOT)
        report = logs.logs(device, params(package='not.installed', since='5:'), None)
        self.assertEqual(report, {'lines': [], 'cursor': '5:', 'truncated': False, 'unparsed': 0,
                                  'note': 'not.installed is not running. Open the app to see its log lines.'})
        self.assertEqual(device.argv(), [])
        # An unreadable process list means "not running" too, never another app's lines.
        self.assertEqual(logs.logs(Device(BOOT, ps=b'garbage\n\xff\n'), params(package='com.android.launcher3'), None)['lines'], [])


class Validation(unittest.TestCase):
    def test_every_value_is_checked_before_android_is_asked(self):
        bad = [params(lines=49), params(lines=2001), params(lines='500'), params(lines=True), params(lines=500.0),
               params(level='S'), params(level='w'), params(level='*:V'), params(level=None), params(level=['V']),
               params(package=''), params(package='nodots'), params(package='com..x'), params(package='com.example;id'),
               params(package='com.example app'), params(package='-com.example'), params(package='com.1x'),
               params(package='com.' + 'a' * 252), params(package=5), params(package='com.example\n'),
               params(since=''), params(since='abc'), params(since='5'), params(since='5:xyz'), params(since='5:0123456'),
               params(since='5:01234567,'), params(since=5), params(since='1' * 21 + ':'), params(since='5:' + ','.join(['01234567'] * 513)),
               {'lines': 500, 'level': 'V', 'package': None}, {**params(), 'path': '/etc'}, None, []]
        for value in bad:
            device = Device(BOOT)
            with self.assertRaises(RuntimeError, msg=value):
                logs.logs(device, value, None)
            self.assertEqual(device.calls, [], value)
        logs.logs(Device(BOOT), params(package='com.' + 'a' * 251, since='5:' + ','.join(['01234567'] * 512)), None)
        with self.assertRaisesRegex(RuntimeError, 'descriptor'):
            logs.logs(Device(BOOT), params(), 7)

    def test_request_values_never_join_a_command_string(self):
        device = Device(BOOT)
        logs.logs(device, params(package='com.android.launcher3', level='E', since='1795077058189000000:'), None)
        for _, args, _ in device.calls:
            self.assertTrue(all(isinstance(arg, str) and ' ' not in arg for arg in args))
        self.assertNotIn('com.android.launcher3', ' '.join(device.argv()[0]))

    def test_dispatch_checks_first_then_adds_the_device(self):
        asked = []

        def query(kind, values):
            asked.append((kind, values))
            return logs.logs(Device(BOOT), values, None)
        request = {'op': 'logs_get', 'id': ID, **params()}
        report = logs.dispatch(None, request, 1000, query)
        self.assertEqual(asked, [('logs', params())])
        self.assertEqual([report['runtime_id'], len(report['lines'])], [ID, 2])
        self.assertEqual(logs.FIELDS, {'logs_get': {'op', 'id', 'lines', 'level', 'package', 'since'}})
        self.assertEqual(set(logs.QUERIES), {'logs'})
        for invalid in ({**request, 'lines': 5000}, {**request, 'package': 'x'}, {**request, 'extra': 1},
                        {key: value for key, value in request.items() if key != 'since'}, {**request, 'op': 'logs_clear'}):
            with self.assertRaises(RuntimeError):
                logs.dispatch(None, invalid, 1000, query)
        self.assertEqual(len(asked), 1)
        with self.assertRaisesRegex(RuntimeError, 'invalid log answer'):
            logs.dispatch(None, request, 1000, lambda kind, values: 'no')


if __name__ == '__main__':
    unittest.main()
