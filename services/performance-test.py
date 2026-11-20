#!/usr/bin/env python3
"""Inert tests for the live activity query (runtime-performance.py).

The samples are real output of System Waydroid (Android 13)."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('runtime_performance', Path(__file__).with_name('runtime-performance.py'))
performance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(performance)

TAIL = (' 18446744073709551615 93875934452016 93875934456224 140727847805072 0 0 0 4612 1 1073775864 0 0 0 17 3 0 0 0 0 0'
        ' 93875934460320 93875934461584 93876324712448 140727847813549 140727847813648 140727847813648 140727847817182 0')
STAT_INIT = ('1 (init) S 0 1 1 0 -1 4194560 8136 267162 38 984 150 203 372 268 20 0 2 0 199514400 11152089088 1649'
             ' 18446744073709551615 98669207663840 98669209186928 140725684638096 0 0 0 81920 0 1073779960 0 0 0 17 4 0 0 0 0 0'
             ' 98669209212256 98669209212704 98669765967872 140725684641548 140725684641578 140725684641578 140725684641767 0')
STAT_LAUNCHER = '1016 (droid.launcher3) S 75 75 1 0 -1 4194624 23262 0 178 0 65 29 0 0 10 -10 62 0 199514931 14609285120 40182' + TAIL
STAT_SERVER = '361 (system_server) S 75 75 1 0 -1 4194624 213166 0 944 0 1019 430 0 0 18 -2 193 0 199514658 15889567744 78163' + TAIL
STAT_GMS = '680 (gle.android.gms) S 75 75 1 0 -1 4194624 341668 0 1397 0 1036 223 0 0 20 0 89 0 199514802 14934200320 77901' + TAIL
STAT_KERNEL = '2 (kthreadd) S 0 0 0 0 -1 2129984 0 0 0 0 0 5 0 0 20 0 1 0 0 0 0' + TAIL
PS = '''    PID   UID    RSS NAME
      1     0   6596 init
    361  1000 312652 system_server
    680 10094 311604 com.google.android.gms
   1016 10129 160728 com.android.launcher3
   1715 10097 387072 com.google.android.googlequicksearchbox:search
'''
FOCUS = '''  mCurrentFocus=Window{bdddbca u0 com.android.launcher3/com.android.launcher3.uioverrides.QuickstepLauncher}
  mFocusedApp=ActivityRecord{c483c41 u0 com.android.launcher3/.uioverrides.QuickstepLauncher} t6}
'''
GFXINFO = '''Applications Graphics Acceleration Info:
Uptime: 2129040372 Realtime: 2155985601

** Graphics info for pid 1016 [com.android.launcher3] **

Stats since: 2128583116032086ns
Total frames rendered: 3
Janky frames: 2 (66.67%)
Janky frames (legacy): 2 (66.67%)
50th percentile: 32ms
90th percentile: 1650ms
Number Missed Vsync: 1
'''
LAUNCHER = 'com.android.launcher3'
ACTIVITY = 'com.android.launcher3.uioverrides.QuickstepLauncher'
WINDOW_LAYER = LAUNCHER + '/' + ACTIVITY + '#57'
LAYERS = 'BootAnimation#1\nbbq-wrapper#2\nc483c41 ' + WINDOW_LAYER[:-3] + '#56\n' + WINDOW_LAYER + '\n'
INT64_MAX = 9223372036854775807


def snapshot(header='uptime 2156021.35 9383960.66\nticks 100\npage 4096\ncpus 6\n',
             stats='\n'.join((STAT_INIT, STAT_LAUNCHER, STAT_SERVER, STAT_GMS, STAT_KERNEL)) + '\n',
             focus=FOCUS, listing=PS, layers=LAYERS, markers=5):
    text = ''
    for index, part in enumerate((header, stats, focus, listing, layers), 1):
        text += part + (f'ANVIL_PERF_{index}\n' if index <= markers else '')
    return text.encode()


def latency(start, count, step=13513513, period='13513513'):
    rows = [f'{start + step * index - 1000}\t{start + step * index}\t{start + step * index + 500}' for index in range(count)]
    return '\n'.join([period, *rows, '']).encode()


class Android:
    """A stand-in for run(): answers by argv and records every call."""
    def __init__(self, snap=None, gfx=GFXINFO.encode(), layers=None):
        self.snap, self.gfx, self.layers, self.calls = snapshot() if snap is None else snap, gfx, layers or {}, []

    def __call__(self, tool, *args, timeout=8, limit=256 * 1024, sink=None, tail=False, check=True):
        self.calls.append((tool, *args))
        assert all(isinstance(item, str) and item for item in args) and 0 < timeout <= 20
        if tool == 'sh':
            answer = self.snap
        elif args[-2:-1] == ('gfxinfo',):
            answer = self.gfx
        else:
            answer = self.layers.get(args[-1], b'13513513\n')
        if isinstance(answer, Exception):
            raise answer
        return answer if check else (0, answer)


class Stat(unittest.TestCase):
    def test_real_lines(self):
        self.assertEqual(performance.parse_stat(STAT_LAUNCHER),
                         {'pid': 1016, 'comm': 'droid.launcher3', 'ticks': 94, 'start': 199514931,
                          'vsize': 14609285120, 'pages': 40182})
        self.assertEqual(performance.parse_stat(STAT_INIT)['ticks'], 353)

    def test_names_with_spaces_and_parentheses(self):
        for comm in ('a b', 'x) S 1 2 3', '((weird))', ') R 0 0 0 0 0 0 0 0 0 0 99 99', '', 'Web Content (2)'):
            line = STAT_LAUNCHER.replace('(droid.launcher3)', '(' + comm + ')')
            entry = performance.parse_stat(line)
            self.assertEqual((entry['comm'], entry['ticks'], entry['start']), (comm, 94, 199514931), comm)

    def test_broken_lines_are_skipped(self):
        for line in ('', 'garbage', '12 (sh', '12 sh) S 1', '(sh) S 0 0', 'x (sh) S' + ' 1' * 30, '-5 (sh) S' + ' 1' * 30,
                     '12 (sh) S 1 2 3', '12 (sh) S' + ' 1' * 10 + ' -4' + ' 1' * 20, '12 (sh) S' + ' 1' * 10 + ' x' * 20,
                     '1' * 11 + ' (sh) S' + ' 1' * 30):
            self.assertIsNone(performance.parse_stat(line), line)


class Processes(unittest.TestCase):
    def test_header_reads_the_clock_and_falls_back(self):
        self.assertEqual(performance.parse_header('uptime 2156021.35 9383960.66\nticks 100\npage 4096\ncpus 6\n'),
                         {'uptime': 2156021.35, 'ticks': 100, 'page': 4096, 'cpus': 6})
        self.assertEqual(performance.parse_header('uptime 5.5 1\nticks \npage x\ncpus 0\n'),
                         {'uptime': 5.5, 'ticks': 100, 'page': 4096, 'cpus': None})
        for bad in (None, '', 'ticks 100\n', 'uptime \n', 'uptime soon\n', 'uptime nan\n', 'uptime inf\n', 'uptime -1\n'):
            self.assertIsNone(performance.parse_header(bad), bad)

    def test_ps_rows(self):
        rows = performance.parse_ps(PS + 'not a row\n 12 x 5 name\n   77 10001   12 my app (beta)  \n')
        self.assertEqual(rows[1016], (10129, 160728, 'com.android.launcher3'))
        self.assertEqual(rows[1715][2], 'com.google.android.googlequicksearchbox:search')
        self.assertEqual(rows[77], (10001, 12, 'my app (beta)'))
        self.assertEqual(len(rows), 6)
        self.assertEqual(performance.parse_ps(None), {})
        self.assertEqual(performance.parse_ps('   9 10001 5 ' + 'n' * 500 + '\n')[9][2], 'n' * 128)
        self.assertEqual(performance.parse_ps('   9 10001 5 a\x1b[31mb\n')[9][2], 'a?[31mb')

    def test_join_uses_ps_names_and_skips_kernel_threads(self):
        rows = performance.parse_processes('\n'.join((STAT_INIT, STAT_LAUNCHER, STAT_KERNEL, 'junk')), PS)
        self.assertEqual([row['pid'] for row in rows], [1016, 1])
        self.assertEqual(rows[0], {'pid': 1016, 'start': 199514931, 'ticks': 94, 'uid': 10129, 'rss_kb': 160728,
                                   'name': 'com.android.launcher3'})

    def test_without_ps_the_stat_line_is_enough(self):
        rows = performance.parse_processes(STAT_LAUNCHER + '\n', None)
        self.assertEqual(rows, [{'pid': 1016, 'start': 199514931, 'ticks': 94, 'uid': None, 'rss_kb': 160728,
                                 'name': 'droid.launcher3'}])
        self.assertIsNone(performance.parse_processes(None, PS))
        self.assertIsNone(performance.parse_processes('nothing\n', PS))

    def test_the_list_is_bounded(self):
        stats = '\n'.join(STAT_LAUNCHER.replace('1016 ', str(index) + ' ', 1) for index in range(1, 900))
        self.assertEqual(len(performance.parse_processes(stats, None)), performance.MAX_PROCESSES)


class Focus(unittest.TestCase):
    def test_real_sample(self):
        self.assertEqual(performance.parse_focus(FOCUS), {'package': LAUNCHER, 'activity': ACTIVITY})

    def test_format_variants(self):
        game = {'package': 'com.example.game', 'activity': 'com.example.game.Main'}
        for text in (
                '  mFocusedApp=ActivityRecord{c483c41 u0 com.example.game/.Main t6}\n',                         # Android 11
                '  mFocusedApp=AppWindowToken{1 token=Token{2 ActivityRecord{3 u0 com.example.game/.Main t6}}}\n',  # Android 10
                '  mCurrentFocus=null\n  mFocusedApp=ActivityRecord{c483c41 u0 com.example.game/.Main} t6}\n',
                '  mCurrentFocus=Window{a1 u0 PopupWindow:9f3e2}\n  mFocusedApp=ActivityRecord{c u0 com.example.game/.Main} t6}\n',
                '  mCurrentFocus=Window{a1 u0 NotificationShade}\n  mFocusedApp=ActivityRecord{c u10 com.example.game/.Main} t6}\n',
                '  mCurrentFocus=Window{a1 u0 com.example.game/com.example.game.Main}\n',
                '  mCurrentFocus=Window{a1 u0 com.example.game/com.example.game.Main}\n  mFocusedApp=null\n',
                # A dialog of another app has input focus: the activity in front still counts.
                '  mCurrentFocus=Window{a1 u0 com.android.permissioncontroller/com.android.permissioncontroller.Grant}\n'
                '  mFocusedApp=ActivityRecord{c u0 com.example.game/.Main} t6}\n',
                # Two displays: the first answer wins.
                '  mFocusedApp=ActivityRecord{c u0 com.example.game/.Main} t6}\n  mFocusedApp=ActivityRecord{d u0 org.other.app/.Home} t7}\n'):
            self.assertEqual(performance.parse_focus(text), game, text)

    def test_nothing_in_front(self):
        for text in (None, '', '  mCurrentFocus=null\n  mFocusedApp=null\n', '  mCurrentFocus=Window{a1 u0 StatusBar}\n',
                     '  mFocusedApp=ActivityRecord{c u0 nodots/.Main} t6}\n', '  mFocusedApp=ActivityRecord{c u0 com.x;rm/.Main}\n',
                     '  mFocusedApp=ActivityRecord{c u0 ' + 'a.' * 150 + 'b/.Main} t6}\n', '  other=Window{a u0 com.example.game/.Main}\n'):
            self.assertIsNone(performance.parse_focus(text), text)

    def test_package_pattern(self):
        for good in ('com.android.launcher3', 'a.b', 'org.Example_1.app2'):
            self.assertTrue(performance.valid_package(good))
        for bad in ('', 'single', '.a.b', 'a..b', 'a.b.', 'a.1b', 'a b.c', 'a.b;c', '-a.b', 'a.b/c', 'a.b\n', None, 5, 'a.' * 120 + 'b'):
            self.assertFalse(performance.valid_package(bad), bad)


class Frames(unittest.TestCase):
    def test_real_sample(self):
        self.assertEqual(performance.parse_gfxinfo(GFXINFO, LAUNCHER, 7),
                         {'pid': 1016, 'total': 3, 'since': '2128583116032086', 'uptime_ms': 2129040372})

    def test_missing_process_or_counter(self):
        self.assertIsNone(performance.parse_gfxinfo('No process found for: not.installed.pkg\n', 'not.installed.pkg', 7))
        self.assertIsNone(performance.parse_gfxinfo('', LAUNCHER, 7))
        self.assertIsNone(performance.parse_gfxinfo(GFXINFO.replace('Total frames rendered: 3\n', ''), LAUNCHER, 7))
        self.assertIsNone(performance.parse_gfxinfo(GFXINFO.replace('rendered: 3', 'rendered: -3'), LAUNCHER, 7))
        self.assertIsNone(performance.parse_gfxinfo(GFXINFO, 'com.android.launcher', 7))

    def test_exact_process_wins_over_its_services_and_clock_falls_back(self):
        text = ('** Graphics info for pid 5 [com.example.game:remote] **\n\nTotal frames rendered: 9\n\n'
                '** Graphics info for pid 6 [com.example.game] **\n\nStats since: 44ns\nTotal frames rendered: 120\n'
                '** Graphics info for pid 7 [com.example.gamepad] **\n\nTotal frames rendered: 999\n')
        self.assertEqual(performance.parse_gfxinfo(text, 'com.example.game', 7), {'pid': 6, 'total': 120, 'since': '44', 'uptime_ms': 7})
        only = performance.parse_gfxinfo(text.replace('[com.example.game]', '[other.app]'), 'com.example.game', 7)
        self.assertEqual((only['pid'], only['total'], only['since']), (5, 9, ''))


class Layers(unittest.TestCase):
    GAME = 'com.example.game'
    LIST = '\n'.join((
        'Display 0 name="Built-in"#3', 'Task=6#40', 'ActivityRecord{c483c41 u0 com.example.game/.Main t6}#41',
        'c483c41 com.example.game/com.example.game.Main#42', 'com.example.game/com.example.game.Main#43',
        'SurfaceView[com.example.game/com.example.game.Main]#44', 'SurfaceView[com.example.game/com.example.game.Main](BLAST)#45',
        'Background for SurfaceView[com.example.game/com.example.game.Main]#46', 'Bounds for - com.example.game/com.example.game.Main#47',
        'com.example.game/com.example.game.Settings#48', 'com.example.gamepad/com.example.gamepad.Main#49',
        'SurfaceView - com.example.game/com.example.game.Main#0', 'Splash Screen com.example.game#50',
        'com.example.game/' + 'x' * 300 + '#51', 'com.example.game/no.number', 'com.example.game/bad\x07name#52', ''))

    def test_order_prefers_surface_views_then_the_window_in_front(self):
        self.assertEqual(performance.layer_candidates(self.LIST, self.GAME, 'com.example.game.Main'), [
            'SurfaceView[com.example.game/com.example.game.Main](BLAST)#45',
            'SurfaceView - com.example.game/com.example.game.Main#0',
            'SurfaceView[com.example.game/com.example.game.Main]#44',
            'com.example.game/com.example.game.Main#43',
            'com.example.game/com.example.game.Settings#48'])

    def test_every_candidate_is_a_listed_line(self):
        listed = set(self.LIST.split('\n'))
        for package in (self.GAME, 'com.example.gamepad', 'com.example', 'Splash Screen com.example.game'):
            for name in performance.layer_candidates(self.LIST, package):
                self.assertIn(name, listed)
                self.assertLessEqual(len(name), performance.MAX_LAYER)
        self.assertEqual(performance.layer_candidates(self.LIST, 'com.example.gamepad'), ['com.example.gamepad/com.example.gamepad.Main#49'])
        self.assertEqual(performance.layer_candidates(self.LIST, 'com.example'), [])
        self.assertEqual(performance.layer_candidates(None, self.GAME), [])
        self.assertEqual(performance.layer_candidates("Can't find service: SurfaceFlinger\n", self.GAME), [])

    def test_real_list_has_the_launcher_window(self):
        self.assertEqual(performance.layer_candidates(LAYERS, LAUNCHER, ACTIVITY), [WINDOW_LAYER])


class Latency(unittest.TestCase):
    def test_real_sample_of_a_layer_without_frames(self):
        self.assertEqual(performance.parse_latency('13513513\n'), {'refresh_hz': 74.0, 'fps': None, 'newest': None, 'frames': 0})

    def test_steady_frames(self):
        entry = performance.parse_latency(latency(2128583116032086, 127, step=16666667, period='16666667').decode())
        self.assertEqual((entry['refresh_hz'], entry['fps'], entry['frames']), (60.0, 60.0, 127))
        self.assertEqual(entry['newest'], str(2128583116032086 + 16666667 * 126))
        self.assertEqual(performance.parse_latency(latency(10**15, 40, step=33333333).decode())['fps'], 30.0)

    def test_unused_and_pending_rows_are_ignored(self):
        rows = ['16666667', '0\t0\t0', '0\t0\t0', f'5000000000\t{INT64_MAX}\t{INT64_MAX}', '4000000000\t4000000000\t4000000100',
                '4100000000\t4100000000\t4100000100', f'4200000000\t4200000000\t{INT64_MAX}', 'x\ty\tz', '1 2', '-5\t-5\t-5', '']
        entry = performance.parse_latency('\n'.join(rows))
        self.assertEqual(entry, {'refresh_hz': 60.0, 'fps': 10.0, 'newest': '4200000000', 'frames': 3})
        self.assertEqual(performance.parse_latency('16666667\n' + '0\t0\t0\n' * 127)['fps'], None)

    def test_only_the_last_second_counts_and_one_frame_is_one(self):
        old = [f'{t}\t{t}\t{t}' for t in (10**9, 10**9 + 10**7, 10**9 + 2 * 10**7)]
        new = [f'{t}\t{t}\t{t}' for t in (9 * 10**9, 9 * 10**9 + 5 * 10**8)]
        self.assertEqual(performance.parse_latency('\n'.join(['16666667', *old, *new]))['fps'], 2.0)
        self.assertEqual(performance.parse_latency('\n'.join(['16666667', *old, new[0]]))['fps'], 1.0)

    def test_no_service_and_bad_period(self):
        self.assertIsNone(performance.parse_latency("Can't find service: SurfaceFlinger\n"))
        self.assertIsNone(performance.parse_latency(''))
        self.assertIsNone(performance.parse_latency('0\n'))
        self.assertIsNone(performance.parse_latency('99999999999999\n'))
        self.assertEqual(performance.parse_latency('5\n7\t8\t9\n')['refresh_hz'], None)


class Collect(unittest.TestCase):
    def test_whole_poll_from_the_real_samples(self):
        android = Android(layers={WINDOW_LAYER: latency(2128583116032086, 20)})
        report = performance.collect(android, {}, None)
        self.assertEqual((report['uptime'], report['ticks'], report['cpus']), (2156021.35, 100, 6))
        self.assertEqual([row['name'] for row in report['processes']],
                         ['system_server', 'com.google.android.gms', 'com.android.launcher3', 'init'])
        self.assertEqual(report['focus'], {'package': LAUNCHER, 'activity': ACTIVITY})
        self.assertEqual(report['frames'], {'pid': 1016, 'total': 3, 'since': '2128583116032086', 'uptime_ms': 2129040372})
        self.assertEqual((report['surface']['layer'], report['surface']['kind'], report['surface']['refresh_hz'],
                          report['surface']['fps']), (WINDOW_LAYER, 'window', 74.0, 74.0))
        self.assertEqual(report['unavailable'], {})
        json.dumps(report, allow_nan=False)
        # Three attaches. Only the constant reaches a shell; the package and the layer are single argv items.
        self.assertEqual(android.calls, [('sh', '-c', performance.SNAPSHOT),
                                         ('dumpsys', '-t', '2', 'gfxinfo', LAUNCHER),
                                         ('dumpsys', '-t', '1', 'SurfaceFlinger', '--latency', WINDOW_LAYER)])

    def test_parameters_are_refused(self):
        for params, sink in (({'package': 'a.b'}, None), ({}, 5), (None, None), ([], None)):
            android = Android()
            with self.assertRaisesRegex(RuntimeError, 'Invalid activity request'):
                performance.collect(android, params, sink)
            self.assertEqual(android.calls, [])

    def test_snapshot_failure_is_the_only_hard_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            performance.collect(Android(snap=RuntimeError('Android sh timed out')), {}, None)
        with self.assertRaisesRegex(RuntimeError, 'did not report its clock'):
            performance.collect(Android(snap=b''), {}, None)

    def test_each_part_degrades_alone(self):
        # No focus lines: processes still arrive, nothing is asked about any package.
        android = Android(snap=snapshot(focus=''))
        report = performance.collect(android, {}, None)
        self.assertEqual(len(report['processes']), 4)
        self.assertEqual((report['focus'], report['frames'], report['surface']), (None, None, None))
        self.assertEqual(report['unavailable'], dict.fromkeys(('focus', 'frames', 'surface'), performance.NO_FOCUS_REPORT))
        self.assertEqual(len(android.calls), 1)
        report = performance.collect(Android(snap=snapshot(focus='  mCurrentFocus=null\n  mFocusedApp=null\n')), {}, None)
        self.assertEqual(report['unavailable']['focus'], performance.NO_FOCUS)

        # The output stopped after the process table.
        report = performance.collect(Android(snap=snapshot(markers=2)), {}, None)
        self.assertEqual(len(report['processes']), 4)
        self.assertEqual(report['processes'][0]['uid'], None)
        self.assertEqual(report['focus'], None)

        # No process table, but frames still work.
        report = performance.collect(Android(snap=snapshot(stats=''), layers={WINDOW_LAYER: latency(10**15, 5)}), {}, None)
        self.assertEqual(report['processes'], None)
        self.assertEqual(report['unavailable'], {'processes': performance.NO_PROCESSES})
        self.assertEqual(report['frames']['total'], 3)

        # gfxinfo fails or has no such process: the surface still answers, and the other way round.
        for gfx, reason in ((RuntimeError('Android dumpsys timed out'), performance.NO_FRAMES_ANSWER),
                            (b'No process found for: com.android.launcher3\n', performance.NO_FRAMES)):
            report = performance.collect(Android(gfx=gfx, layers={WINDOW_LAYER: latency(10**15, 5)}), {}, None)
            self.assertEqual((report['frames'], report['unavailable']), (None, {'frames': reason}))
            self.assertEqual(report['surface']['frames'], 5)
        report = performance.collect(Android(layers={WINDOW_LAYER: OSError('lxc-attach')}), {}, None)
        self.assertEqual((report['surface'], report['unavailable']), (None, {'surface': performance.NO_SURFACE_ANSWER}))
        report = performance.collect(Android(layers={WINDOW_LAYER: b"Can't find service: SurfaceFlinger\n"}), {}, None)
        self.assertEqual((report['surface'], report['unavailable']), (None, {'surface': performance.NO_SURFACE}))
        android = Android(snap=snapshot(layers="Can't find service: SurfaceFlinger\n"))
        report = performance.collect(android, {}, None)
        self.assertEqual((report['surface'], report['unavailable']), (None, {'surface': performance.NO_SURFACE}))
        self.assertEqual(len(android.calls), 2)

    def test_idle_layer_still_reports_the_refresh_rate(self):
        report = performance.collect(Android(), {}, None)
        self.assertEqual(report['surface'], {'refresh_hz': 74.0, 'fps': None, 'newest': None, 'frames': 0,
                                             'layer': WINDOW_LAYER, 'kind': 'window'})
        self.assertNotIn('surface', report['unavailable'])

    def test_the_layer_that_presented_last_wins_and_queries_are_capped(self):
        game = 'com.example.game'
        names = [f'SurfaceView[{game}/{game}.Main](BLAST)#45', f'{game}/{game}.Main#43', f'{game}/{game}.Menu#48', f'{game}/{game}.Old#12']
        focus = f'  mFocusedApp=ActivityRecord{{c u0 {game}/.Main}} t6}}\n'
        android = Android(snap=snapshot(focus=focus, layers='\n'.join(names) + '\n'), gfx=b'No process found for: x\n',
                          layers={names[0]: latency(5 * 10**12, 60, step=16666667), names[1]: latency(4 * 10**12, 10),
                                  names[2]: latency(9 * 10**12, 10), names[3]: latency(3 * 10**12, 10)})
        report = performance.collect(android, {}, None)
        self.assertEqual((report['surface']['layer'], report['surface']['kind'], report['surface']['fps']), (names[0], 'surfaceview', 60.0))
        asked = [call[-1] for call in android.calls if '--latency' in call]
        # Three layers at most are asked in one poll.
        self.assertEqual(asked, [names[0], names[1], names[3]])
        android.layers[names[1]] = latency(6 * 10**12, 10)
        self.assertEqual(performance.collect(android, {}, None)['surface']['layer'], names[1])

    def test_hostile_layer_names_stay_one_argument(self):
        game = 'com.example.game'
        nasty = f'SurfaceView[{game}/x] $(reboot); `id` "q" \' #7 --list#9'
        android = Android(snap=snapshot(focus=f'  mFocusedApp=ActivityRecord{{c u0 {game}/.Main}} t6}}\n', layers=nasty + '\n'))
        performance.collect(android, {}, None)
        self.assertEqual(android.calls[-1], ('dumpsys', '-t', '1', 'SurfaceFlinger', '--latency', nasty))
        self.assertEqual(android.calls[0], ('sh', '-c', performance.SNAPSHOT))
        self.assertNotIn(game, performance.SNAPSHOT)

    def test_time_budget_skips_the_late_parts(self):
        clock = iter([0.0, 4.9, 4.95, 5.0, 5.0])
        with mock.patch.object(performance.time, 'monotonic', lambda: next(clock)):
            android = Android(layers={WINDOW_LAYER: latency(10**15, 5)})
            report = performance.collect(android, {}, None)
        self.assertEqual(len(android.calls), 1)
        self.assertEqual(report['unavailable'], {'frames': performance.NO_TIME, 'surface': performance.NO_SURFACE_ANSWER})
        self.assertEqual(len(report['processes']), 4)

    def test_a_marker_inside_a_name_does_not_split_the_snapshot(self):
        text = 'a\nANVIL_PERF_1\n1 (x ANVIL_PERF_2) S\nANVIL_PERF_22\nANVIL_PERF_2\nANVIL_PERF_3\nps\nANVIL_PERF_4\nANVIL_PERF_5\n'
        self.assertEqual(performance.sections(text), ['a', '1 (x ANVIL_PERF_2) S\nANVIL_PERF_22', '', 'ps', ''])

    def test_snapshot_is_a_constant_without_format_fields(self):
        self.assertEqual(performance.SNAPSHOT.count('%'), 1)        # only printf's own '%s'
        self.assertIn("printf '%s\\n' \"$l\"", performance.SNAPSHOT)   # echo would interpret backslashes in names
        self.assertNotIn('{', performance.SNAPSHOT)
        self.assertNotIn('screenrecord', performance.SNAPSHOT)
        self.assertNotIn('top ', performance.SNAPSHOT)


class Dispatch(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(performance.FIELDS, {'performance_get': {'op', 'id'}})
        self.assertEqual(set(performance.QUERIES), {'performance'})
        self.assertFalse(getattr(performance, 'DESCRIPTOR_OPS', None))

    def test_routes_one_query_and_names_the_device(self):
        asked = []

        def query(kind, params, sink=None):
            asked.append((kind, params, sink))
            return performance.collect(Android(), params, sink)
        result = performance.dispatch(None, {'op': 'performance_get', 'id': 'default'}, 1000, query)
        self.assertEqual(asked, [('performance', {}, None)])
        self.assertEqual(result['runtime_id'], 'default')
        self.assertEqual(result['focus']['package'], LAUNCHER)
        self.assertLess(len(json.dumps(result)), 64 * 1024)

    def test_refuses_other_shapes_and_bad_answers(self):
        for request in ({'op': 'performance_get', 'id': 'default', 'package': 'a.b'}, {'op': 'performance_set', 'id': 'default'},
                        {'op': 'performance_get'}):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
                performance.dispatch(None, request, 1000, lambda *args: self.fail('queried'))
        for answer in (None, [], {'processes': []}, {'unavailable': 'no'}):
            with self.assertRaisesRegex(RuntimeError, 'invalid activity report'):
                performance.dispatch(None, {'op': 'performance_get', 'id': 'default'}, 1000, lambda *args: answer)

    def test_a_full_device_stays_far_below_the_answer_limit(self):
        stats = '\n'.join(STAT_LAUNCHER.replace('1016 ', str(index) + ' ', 1) for index in range(1, 900)) + '\n'
        listing = ''.join(f'{index:7d} 10001 160728 {"n" * 200}\n' for index in range(1, 900))
        report = performance.collect(Android(snap=snapshot(stats=stats, listing=listing)), {}, None)
        self.assertLess(len(json.dumps(report)), 128 * 1024)


if __name__ == '__main__':
    unittest.main()
