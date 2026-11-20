#!/usr/bin/env python3
"""Inert tests for the device proxy and Private DNS (runtime-proxy.py): a fake `settings` tool, no Android."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


proxy = module('runtime-proxy')
proxy.sleep = lambda seconds: None
ID = 'r-' + '1' * 32
AUTOMATIC = {'mode': 'automatic', 'hostname': ''}
OFF = {'mode': 'off', 'hostname': ''}


def hostname(name):
    return {'mode': 'hostname', 'hostname': name}


def direct(host='192.168.1.10', port=3128, bypass=()):
    return {'host': host, 'port': port, 'bypass': list(bypass)}


class Android:
    """The `settings` tool over a dict. Output formats are the ones captured from a real device:
    `get` prints the value or the literal text `null`, each followed by a newline.
    With `mirror`, a write to http_proxy is followed by ConnectivityService's own writes."""
    def __init__(self, values=None, mirror=True, fail=None):
        self.values, self.mirror, self.fail = dict(values or {}), mirror, fail
        self.calls, self.options = [], []

    def run(self, tool, *args, **options):
        self.calls.append((tool, *args))
        self.options.append(options)
        if self.fail and self.fail(args):
            raise RuntimeError('Android settings failed: Failed transaction (2147483646)')
        verb, namespace, key, *rest = args
        assert tool == 'settings' and namespace == 'global' and all(type(item) is str and item for item in args)
        if verb == 'get':
            assert not rest
            value = self.values.get(key)
            return (('null' if value is None else value) + '\n').encode()
        if verb == 'put':
            (value,) = rest
            self.values[key] = value
            if key == 'http_proxy' and self.mirror:
                host, _, port = value.partition(':')
                self.values.update({'global_http_proxy_host': host, 'global_http_proxy_port': port if host else '0',
                                    'global_http_proxy_exclusion_list': '', 'global_proxy_pac_url': ''})
            return b''
        assert verb == 'delete' and not rest
        return f'Deleted {int(self.values.pop(key, None) is not None)} rows\n'.encode()

    @property
    def writes(self):
        return [call[1:] for call in self.calls if call[1] != 'get']


def put(key, value): return ('put', 'global', key, value)
def delete(key): return ('delete', 'global', key)


STORED = {'global_http_proxy_host': '192.168.1.10', 'global_http_proxy_port': '3128',
          'global_http_proxy_exclusion_list': '', 'global_proxy_pac_url': ''}


class Validation(unittest.TestCase):
    def test_proxy_hosts(self):
        for good in ('proxy.example.com', 'PROXY', '192.168.1.10', '127.0.0.1', 'localhost', 'a-b.c-d.example', '0.0.0.0',
                     'a' * 63 + '.example', '.'.join(['a' * 49] * 5) + '.aa'):
            self.assertEqual(proxy.check_proxy(good, 8080, [])['host'], good)
        for bad in ('-proxy', '--help', '-', 'proxy-', 'pro xy', ' proxy', 'proxy\n', 'proxy\t', 'a,b', 'a"b', "a'b", 'a;b', 'a$b',
                    'a/b', 'a_b', 'a..b', '.a', 'a.', 'xn--abc.example', 'user@host', 'host:8080', 'http://proxy', '*.example.com',
                    '::1', '[::1]', 'fe80::1', '2001:db8::1', '256.1.1.1', '1.2.3', '1.2.3.4.5', '01.2.3.4', 'host.123', '0x7f.1',
                    'null', 'NULL', 'a' * 64 + '.example', 'a.' * 127 + 'a', 'prøxy', 'proxy\0', 5, None, b'proxy', ['proxy']):
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                proxy.check_proxy(bad, 8080, [])
        with self.assertRaisesRegex(RuntimeError, 'IPv6 addresses are not supported'):
            proxy.check_proxy('fe80::1', 8080, [])
        self.assertEqual(len('a.' * 127 + 'a'), 255)

    def test_ports(self):
        for good in (1, 80, 65535):
            self.assertEqual(proxy.check_proxy('proxy', good, [])['port'], good)
        for bad in (0, -1, 65536, True, False, 80.0, '80', None, [80]):
            with self.assertRaisesRegex(RuntimeError, 'port', msg=repr(bad)):
                proxy.check_proxy('proxy', bad, [])

    def test_bypass_entries(self):
        good = ['localhost', 'example.com', '*.example.com', '10.0.0.1', '*.internal']
        self.assertEqual(proxy.check_proxy('proxy', 80, good)['bypass'], good)
        self.assertEqual(len(proxy.check_proxy('proxy', 80, [f'h{i}.example' for i in range(32)])['bypass']), 32)
        for bad in (['-x'], ['--user'], ['*'], ['*.'], ['*example.com'], ['a.*.com'], ['*.*.com'], ['*.10.0.0.1'], [''], [' a'],
                    ['a b'], ['a,b'], ['a:1'], ['::1'], ['10.0.0'], ['a"'], ['null'], [5], [None], [['a']], ['a', 'A'],
                    ['a' * 254], [f'h{i}.example' for i in range(33)], 'localhost', None, ('a',)):
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                proxy.check_proxy('proxy', 80, bad)

    def test_no_proxy_is_an_empty_host_with_nothing_else(self):
        self.assertIsNone(proxy.check_proxy('', 0, []))
        for port, bypass in ((8080, []), (0, ['a']), (False, []), (None, []), (0, None)):
            with self.assertRaisesRegex(RuntimeError, 'turn the proxy off'):
                proxy.check_proxy('', port, bypass)

    def test_private_dns(self):
        self.assertEqual(proxy.check_dns('off', ''), OFF)
        self.assertEqual(proxy.check_dns('automatic', ''), AUTOMATIC)
        for good in ('dns.adguard.com', 'one.one.one.one', 'xn--dns-abc.example', 'dns', 'a' * 63 + '.example'):
            self.assertEqual(proxy.check_dns('hostname', good), hostname(good))
        for mode, name in (('hostname', ''), ('hostname', '-dns.example'), ('hostname', '--help'), ('hostname', 'dns.example-'),
                           ('hostname', 'dns_x.example'), ('hostname', 'dns example'), ('hostname', 'dns.example.'),
                           ('hostname', '1.1.1.1'), ('hostname', '2606:4700::1111'), ('hostname', 'https://dns.example'),
                           ('hostname', 'dns.example/q'), ('hostname', 'a,b'), ('hostname', 'null'), ('hostname', 'a' * 64),
                           ('hostname', 'a.' * 127), ('hostname', ('a' * 60 + '.') * 5), ('hostname', 5), ('hostname', None),
                           ('off', 'dns.adguard.com'), ('automatic', 'x'), ('opportunistic', ''), ('Off', ''), ('', ''),
                           (None, ''), (True, ''), (['off'], '')):
            with self.assertRaises(RuntimeError, msg=repr((mode, name))):
                proxy.check_dns(mode, name)

    def test_query_parameters_are_validated_again(self):
        self.assertEqual(proxy.wanted({'proxy': None, 'dns': OFF}), {'proxy': None, 'dns': OFF})
        self.assertEqual(proxy.wanted({'proxy': direct(bypass=['*.lan']), 'dns': hostname('dns.adguard.com')}),
                         {'proxy': direct(bypass=['*.lan']), 'dns': hostname('dns.adguard.com')})
        for bad in (None, [], {}, {'proxy': None}, {'proxy': None, 'dns': OFF, 'user': 'x'}, {'proxy': None, 'dns': None},
                    {'proxy': None, 'dns': {'mode': 'off'}}, {'proxy': None, 'dns': {**OFF, 'x': 1}},
                    {'proxy': '', 'dns': OFF}, {'proxy': {'host': 'p', 'port': 80}, 'dns': OFF},
                    {'proxy': {**direct(), 'password': 'x'}, 'dns': OFF}, {'proxy': direct(host=''), 'dns': OFF},
                    {'proxy': direct(host='-p'), 'dns': OFF}, {'proxy': direct(port=True), 'dns': OFF},
                    {'proxy': direct(bypass=['-x']), 'dns': OFF}, {'proxy': None, 'dns': hostname('-x')}):
            android = Android()
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                proxy.network_set(android.run, bad, None)
            self.assertEqual(android.calls, [])
        with self.assertRaises(RuntimeError):
            proxy.network_set(Android().run, {'proxy': None, 'dns': OFF}, 7)
        for bad in ({'x': 1}, None, []):
            with self.assertRaises(RuntimeError):
                proxy.network_get(Android().run, bad, None)


class Reading(unittest.TestCase):
    def test_unset_keys_print_null_and_mean_no_proxy_and_automatic_dns(self):
        android = Android()
        self.assertEqual(proxy.network_get(android.run, {}, None), {'proxy': None, 'dns': AUTOMATIC, 'error': None})
        self.assertEqual(android.calls, [('settings', 'get', 'global', key) for key in (
            'http_proxy', 'global_http_proxy_host', 'global_http_proxy_port', 'global_http_proxy_exclusion_list',
            'private_dns_mode', 'private_dns_specifier')])
        self.assertTrue(all(0 < options['timeout'] <= 4 and options['limit'] == 4096 for options in android.options))

    def test_values_are_reported_as_android_stores_them(self):
        android = Android({**STORED, 'global_http_proxy_exclusion_list': 'localhost, *.lan,10.0.0.1',
                           'private_dns_mode': 'hostname', 'private_dns_specifier': 'dns.adguard.com'})
        self.assertEqual(proxy.network_get(android.run, {}, None), {
            'proxy': direct(bypass=['localhost', '*.lan', '10.0.0.1']), 'dns': hostname('dns.adguard.com'), 'error': None})

    def test_view_of_every_stored_shape(self):
        empty = dict.fromkeys(('http_proxy', 'global_http_proxy_host', 'global_http_proxy_port',
                               'global_http_proxy_exclusion_list', 'private_dns_mode', 'private_dns_specifier'))
        def view(**values): return proxy.view({**empty, **values})
        # What Android leaves behind after a cleared proxy.
        self.assertIsNone(view(http_proxy=':0', global_http_proxy_host='', global_http_proxy_port='0')['proxy'])
        # Written but not taken over yet, with Android's default port.
        self.assertEqual(view(http_proxy='proxy.lan:8888')['proxy'], direct('proxy.lan', 8888))
        self.assertEqual(view(http_proxy='proxy.lan')['proxy'], direct('proxy.lan', 8080))
        # The split keys are what Android applied, so they win.
        self.assertEqual(view(http_proxy='old:1', **STORED)['proxy'], direct())
        # Values Android itself ignores are no proxy, never an error or a wrong number.
        for values in ({'http_proxy': 'proxy:port'}, {'http_proxy': 'proxy:0'}, {'http_proxy': 'pro xy:80'},
                       {'http_proxy': '<script>:80'}, {'http_proxy': '*:80'},
                       {'global_http_proxy_host': 'proxy'}, {'global_http_proxy_host': 'proxy', 'global_http_proxy_port': '0'},
                       {'global_http_proxy_host': 'proxy', 'global_http_proxy_port': '99999'},
                       {'global_http_proxy_host': 'proxy', 'global_http_proxy_port': '８０'},
                       {'global_http_proxy_host': 'a b', 'global_http_proxy_port': '80'},
                       {'global_http_proxy_host': 'x' * 300, 'global_http_proxy_port': '80'}):
            self.assertIsNone(view(**values)['proxy'], values)
        self.assertEqual(view(**{**STORED, 'global_http_proxy_exclusion_list': 'ok.lan,,<b>,a b, *.x '})['proxy']['bypass'],
                         ['ok.lan', '*.x'])
        for mode, expected in ((None, 'automatic'), ('', 'automatic'), ('opportunistic', 'automatic'), ('off', 'off'),
                               ('hostname', 'hostname'), ('strict', 'automatic')):
            self.assertEqual(view(private_dns_mode=mode)['dns'], {'mode': expected, 'hostname': ''})
        # The stored hostname is shown in every mode, so the field is filled in when the user comes back to it.
        self.assertEqual(view(private_dns_mode='off', private_dns_specifier='dns.adguard.com')['dns'],
                         {'mode': 'off', 'hostname': 'dns.adguard.com'})
        self.assertEqual(view(private_dns_mode='hostname', private_dns_specifier='a b')['dns'], hostname(''))

    def test_output_is_decoded_leniently(self):
        self.assertEqual(proxy.get(lambda *args: b'null\n', 'x'), None)
        self.assertEqual(proxy.get(lambda *args: b'null\r\n', 'x'), None)
        self.assertEqual(proxy.get(lambda *args: b'\n', 'x'), '')
        self.assertEqual(proxy.get(lambda *args: b'', 'x'), '')
        self.assertEqual(proxy.get(lambda *args: b'opportunistic\n', 'x'), 'opportunistic')
        self.assertEqual(proxy.get(lambda *args: b'\xff\xfe\n', 'x'), '��')

    def test_a_failing_read_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, 'Android settings failed'):
            proxy.network_get(Android(fail=lambda args: True).run, {}, None)


class Writing(unittest.TestCase):
    def apply(self, android, wanted_proxy, dns=AUTOMATIC):
        return proxy.network_set(android.run, {'proxy': wanted_proxy, 'dns': dns}, None)

    def test_new_proxy_is_applied_through_http_proxy_which_is_then_removed(self):
        android = Android()
        self.assertEqual(self.apply(android, direct()), {'proxy': direct(), 'dns': AUTOMATIC, 'error': None})
        self.assertEqual(android.writes, [put('http_proxy', '192.168.1.10:3128'), delete('http_proxy')])
        self.assertEqual(android.values, STORED)

    def test_bypass_list_is_written_after_android_took_the_proxy_over(self):
        android = Android()
        wanted = direct('proxy.example.com', 8080, ['localhost', '*.lan', '10.0.0.1'])
        self.assertEqual(self.apply(android, wanted)['proxy'], wanted)
        self.assertEqual(android.writes, [put('http_proxy', 'proxy.example.com:8080'),
                                          put('global_http_proxy_exclusion_list', 'localhost,*.lan,10.0.0.1'),
                                          delete('http_proxy')])
        calls = [call[1:] for call in android.calls]
        self.assertLess(calls.index(('get', 'global', 'global_http_proxy_port'), 6), calls.index(android.writes[1]))
        self.assertNotIn('http_proxy', android.values)

    def test_same_host_and_port_never_rewrites_http_proxy(self):
        # Rewriting it would make Android drop a bypass list that is already live.
        android = Android({**STORED, 'global_http_proxy_exclusion_list': 'a.lan'})
        self.assertEqual(self.apply(android, direct(bypass=['a.lan']))['error'], None)
        self.assertEqual(android.writes, [])
        self.assertEqual(self.apply(android, direct(bypass=['b.lan']))['proxy'], direct(bypass=['b.lan']))
        self.assertEqual(android.writes, [put('global_http_proxy_exclusion_list', 'b.lan')])
        android.calls.clear()
        self.assertEqual(self.apply(android, direct())['proxy'], direct())
        self.assertEqual(android.writes, [delete('global_http_proxy_exclusion_list')])

    def test_changed_port_replaces_the_proxy(self):
        android = Android({**STORED, 'global_http_proxy_exclusion_list': 'a.lan'})
        self.assertEqual(self.apply(android, direct(port=8080))['proxy'], direct(port=8080))
        self.assertEqual(android.writes, [put('http_proxy', '192.168.1.10:8080'), delete('http_proxy')])

    def test_stale_http_proxy_is_removed_first_so_the_write_is_a_change(self):
        # Android ignores a write of the value that is already stored.
        android = Android({'http_proxy': '192.168.1.10:3128'})
        self.assertEqual(self.apply(android, direct())['error'], None)
        self.assertEqual(android.writes, [delete('http_proxy'), put('http_proxy', '192.168.1.10:3128'), delete('http_proxy')])
        android = Android({**STORED, 'http_proxy': '192.168.1.10:3128'})
        self.assertEqual(self.apply(android, direct())['error'], None)
        self.assertEqual(android.writes, [delete('http_proxy')])

    def test_clearing_applies_now_and_leaves_no_key_behind(self):
        android = Android({**STORED, 'global_http_proxy_exclusion_list': 'a.lan'})
        self.assertEqual(self.apply(android, None), {'proxy': None, 'dns': AUTOMATIC, 'error': None})
        self.assertEqual(android.writes, [put('http_proxy', ':0'), delete('global_http_proxy_host'), delete('global_http_proxy_port'),
                                          delete('global_http_proxy_exclusion_list'), delete('global_proxy_pac_url'), delete('http_proxy')])
        self.assertEqual(android.values, {})

    def test_clearing_variants(self):
        # Nothing set: nothing written.
        android = Android()
        self.assertEqual(self.apply(android, None)['error'], None)
        self.assertEqual(android.writes, [])
        # Leftovers of an earlier clear by hand: only removed.
        android = Android({'http_proxy': ':0', 'global_http_proxy_host': '', 'global_http_proxy_port': '0'})
        self.assertEqual(self.apply(android, None)['error'], None)
        self.assertEqual([write[0] for write in android.writes], ['delete'] * 5)
        self.assertEqual(android.values, {})
        # A stored ":0" that Android never acted on is written again as a real change.
        android = Android({**STORED, 'http_proxy': ':0'})
        self.assertEqual(self.apply(android, None)['error'], None)
        self.assertEqual(android.writes[:2], [delete('http_proxy'), put('http_proxy', ':0')])
        self.assertEqual(android.values, {})

    def test_private_dns_modes(self):
        android = Android()
        self.assertEqual(self.apply(android, None, hostname('dns.adguard.com'))['dns'], hostname('dns.adguard.com'))
        # The hostname comes first, so Android never sees hostname mode without one.
        self.assertEqual(android.writes, [put('private_dns_specifier', 'dns.adguard.com'), put('private_dns_mode', 'hostname')])
        android.calls.clear()
        self.assertEqual(self.apply(android, None, hostname('one.one.one.one'))['dns'], hostname('one.one.one.one'))
        self.assertEqual(android.writes, [put('private_dns_specifier', 'one.one.one.one')])
        android.calls.clear()
        self.assertEqual(self.apply(android, None, OFF)['dns'], {'mode': 'off', 'hostname': 'one.one.one.one'})
        self.assertEqual(android.writes, [put('private_dns_mode', 'off')])
        android.calls.clear()
        self.assertEqual(self.apply(android, None, AUTOMATIC)['dns']['mode'], 'automatic')
        self.assertEqual(android.writes, [put('private_dns_mode', 'opportunistic')])
        android.calls.clear()
        self.assertEqual(self.apply(android, None, hostname('one.one.one.one'))['error'], None)
        self.assertEqual(android.writes, [put('private_dns_mode', 'hostname')])
        # Android's default is automatic: asking for automatic on an untouched device writes nothing.
        android = Android()
        self.assertEqual(self.apply(android, None, AUTOMATIC), {'proxy': None, 'dns': AUTOMATIC, 'error': None})
        self.assertEqual(android.writes, [])

    def test_proxy_and_dns_together(self):
        android = Android()
        result = self.apply(android, direct(bypass=['a.lan']), OFF)
        self.assertEqual(result, {'proxy': direct(bypass=['a.lan']), 'dns': OFF, 'error': None})
        self.assertEqual(android.writes, [put('http_proxy', '192.168.1.10:3128'), put('global_http_proxy_exclusion_list', 'a.lan'),
                                          delete('http_proxy'), put('private_dns_mode', 'off')])

    def test_failure_halfway_reports_the_real_state_and_says_partly_applied(self):
        android = Android(fail=lambda args: args[0] == 'put' and args[2] == 'global_http_proxy_exclusion_list')
        result = self.apply(android, direct(bypass=['a.lan']), OFF)
        self.assertEqual(result['proxy'], direct())
        self.assertEqual(result['dns'], AUTOMATIC)
        self.assertRegex(result['error'], r'^The network settings may be partly applied\. Android settings failed: Failed transaction')
        # Nothing after the failed step was attempted.
        self.assertEqual(android.writes, [put('http_proxy', '192.168.1.10:3128'), put('global_http_proxy_exclusion_list', 'a.lan')])

        android = Android(STORED, fail=lambda args: args[:3] == ('put', 'global', 'private_dns_mode'))
        result = self.apply(android, None, OFF)
        self.assertEqual((result['proxy'], result['dns']), (None, AUTOMATIC))
        self.assertIn('may be partly applied', result['error'])

    def test_android_that_does_not_react_is_reported(self):
        naps = []
        with mock.patch.object(proxy, 'sleep', naps.append):
            android = Android(mirror=False)
            result = self.apply(android, direct(bypass=['a.lan']))
        self.assertEqual(naps, [proxy.POLL_DELAY] * (proxy.POLLS - 1))
        self.assertRegex(result['error'], 'partly applied.*did not confirm the new proxy')
        self.assertEqual(result['proxy'], direct())                      # what the next start would use
        self.assertEqual(android.writes, [put('http_proxy', '192.168.1.10:3128')])
        android = Android(STORED, mirror=False)
        result = self.apply(android, None)
        self.assertRegex(result['error'], 'partly applied.*did not confirm that the proxy is off')
        self.assertEqual(android.values, {})                              # still nothing left for the next start
        self.assertIsNone(result['proxy'])

    def test_a_value_android_did_not_keep_is_an_error_with_the_real_state(self):
        class Forgetful(Android):
            def run(self, tool, *args, **options):
                result = super().run(tool, *args, **options)
                if args[:3] == ('put', 'global', 'global_http_proxy_exclusion_list'):
                    self.values['global_http_proxy_exclusion_list'] = ''
                return result
        result = self.apply(Forgetful(), direct(bypass=['a.lan']))
        self.assertEqual(result['proxy'], direct())
        self.assertRegex(result['error'], 'did not keep every setting')

    def test_unreadable_state_after_a_failure_raises_with_both_reasons(self):
        seen = []
        def fail(args):
            seen.append(args)
            return args[0] == 'put' or len(seen) > 8
        with self.assertRaisesRegex(RuntimeError, r'may be partly applied\. Android settings failed.*could not be read'):
            self.apply(Android(fail=fail), direct())

    def test_one_time_budget_covers_the_whole_series(self):
        clock = [0.0]
        def slow(tool, *args, **options):
            clock[0] += 3.5
            return b'null\n'
        with mock.patch.object(proxy.time, 'monotonic', lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, 'too slowly'):
                proxy.network_get(slow, {}, None)

    def test_no_value_can_be_read_as_an_option(self):
        android = Android()
        self.apply(android, direct('proxy.example.com', 1, ['*.lan']), hostname('dns.adguard.com'))
        for call in android.calls:
            self.assertEqual(call[:3][0], 'settings')
            self.assertIn(call[1], ('get', 'put', 'delete'))
            self.assertFalse(any(item.startswith('-') for item in call[1:]))


class Dispatch(unittest.TestCase):
    def request(self, **changes):
        return {'op': 'network_set', 'id': ID, 'proxy_host': '192.168.1.10', 'proxy_port': 3128, 'proxy_bypass': ['*.lan'],
                'dns_mode': 'hostname', 'dns_hostname': 'dns.adguard.com', **changes}

    def test_fields_and_queries_are_declared(self):
        self.assertEqual(proxy.FIELDS, {'network_get': {'op', 'id'}, 'network_set': {
            'op', 'id', 'proxy_host', 'proxy_port', 'proxy_bypass', 'dns_mode', 'dns_hostname'}})
        self.assertEqual(set(proxy.QUERIES), {'network_get', 'network_set'})
        self.assertEqual(set(self.request()), proxy.FIELDS['network_set'])
        inspect = module('runtime-inspect')
        self.assertIs(inspect.OWNERS['network_set'], next(f for f in inspect.features if f.__name__ == 'runtime_proxy'))
        self.assertIn('network_get', inspect.QUERIES)

    def test_requests_become_validated_queries(self):
        asked = []
        def query(kind, params):
            asked.append((kind, params))
            return {'proxy': None, 'dns': AUTOMATIC, 'error': None}
        self.assertEqual(proxy.dispatch(None, {'op': 'network_get', 'id': 'default'}, 0, query),
                         {'runtime_id': 'default', 'proxy': None, 'dns': AUTOMATIC, 'error': None})
        self.assertEqual(proxy.dispatch(None, self.request(), 0, query)['runtime_id'], ID)
        proxy.dispatch(None, self.request(proxy_host='', proxy_port=0, proxy_bypass=[], dns_mode='off', dns_hostname=''), 0, query)
        self.assertEqual(asked, [
            ('network_get', {}),
            ('network_set', {'proxy': direct(bypass=['*.lan']), 'dns': hostname('dns.adguard.com')}),
            ('network_set', {'proxy': None, 'dns': OFF})])

    def test_bad_requests_never_reach_android(self):
        def query(kind, params): self.fail('queried')
        for changes in ({'proxy_host': '-x'}, {'proxy_host': 'a b'}, {'proxy_host': '::1'}, {'proxy_host': None},
                        {'proxy_port': True}, {'proxy_port': '3128'}, {'proxy_port': 0}, {'proxy_port': 70000},
                        {'proxy_bypass': ['--x']}, {'proxy_bypass': 'a'}, {'proxy_bypass': ['a'] * 2}, {'proxy_host': ''},
                        {'dns_mode': 'strict'}, {'dns_hostname': ''}, {'dns_hostname': '-x'}, {'dns_mode': 'off'}):
            with self.assertRaises(RuntimeError, msg=repr(changes)):
                proxy.dispatch(None, self.request(**changes), 0, query)

    def test_a_malformed_answer_is_refused(self):
        for answer in (None, [], {}, {'proxy': None, 'dns': AUTOMATIC}, {'proxy': None, 'dns': AUTOMATIC, 'error': None, 'x': 1}):
            with self.assertRaisesRegex(RuntimeError, 'did not answer'):
                proxy.dispatch(None, {'op': 'network_get', 'id': ID}, 0, lambda kind, params: answer)

    def test_set_end_to_end_with_the_fake_tool(self):
        android = Android()
        def query(kind, params): return proxy.QUERIES[kind](android.run, params, None)
        self.assertEqual(proxy.dispatch(None, self.request(), 0, query), {
            'runtime_id': ID, 'proxy': direct(bypass=['*.lan']), 'dns': hostname('dns.adguard.com'), 'error': None})
        self.assertEqual(proxy.dispatch(None, {'op': 'network_get', 'id': ID}, 0, query)['proxy'], direct(bypass=['*.lan']))


if __name__ == '__main__':
    unittest.main()
