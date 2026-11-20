"""Per-device HTTP proxy and Private DNS, kept in Android's own global settings.

Nothing is stored by the controller: Android keeps these values in its data, so
they survive restarts and disappear when the device is reinstalled or its data
is cleared. Every change goes through the `settings` tool with a fixed argv,
only while Android runs (see runtime-inspect.py for the contract).

How Android (11 to 15) reacts to the proxy keys, from ConnectivityService and
ProxyTracker in AOSP:

- `http_proxy` is the ONLY proxy key ConnectivityService observes. On a change
  it parses `host:port`, makes that the live global proxy with an EMPTY
  exclusion list, and then writes the split keys itself:
  `global_http_proxy_host`, `global_http_proxy_port`,
  `global_http_proxy_exclusion_list` (as "") and `global_proxy_pac_url` (as "").
  The value `:0` has an empty host, which clears the live proxy and empties the
  split keys.
- The split keys are not observed. They are read once, when Android starts, and
  that is the only place where the exclusion list is honoured. Newer releases
  look at `http_proxy` first at start and, when it is set, use it again with an
  empty exclusion list.

So the order for a new proxy is:
  1. put `http_proxy host:port`      the proxy applies now
  2. wait until the split keys show it  Android has finished its own writes
  3. put or delete the exclusion list  after step 2, or Android would overwrite it
  4. delete `http_proxy`             the split keys stay the one source at the
                                     next start, so the exclusion list survives
A consequence that cannot be avoided with `settings` alone: the bypass list
takes effect the next time the device starts. When host and port are unchanged,
`http_proxy` is not written at all, because that would drop a bypass list that
is already live.

Clearing is `put http_proxy :0` (applies now), then every proxy key is deleted
so nothing can bring a proxy back at the next start.

Private DNS: ConnectivityService observes both `private_dns_mode` and
`private_dns_specifier`. The hostname is written before the mode, so Android
never sees hostname mode without a hostname.
"""
import re
import time

FIELDS = {
    'network_get': {'op', 'id'},
    'network_set': {'op', 'id', 'proxy_host', 'proxy_port', 'proxy_bypass', 'dns_mode', 'dns_hostname'},
}

LEGACY = 'http_proxy'
HOST = 'global_http_proxy_host'
PORT = 'global_http_proxy_port'
EXCLUSION = 'global_http_proxy_exclusion_list'
PAC = 'global_proxy_pac_url'
DNS_MODE = 'private_dns_mode'
DNS_SPECIFIER = 'private_dns_specifier'
CLEAR = ':0'

MAX_NAME = 253
MAX_BYPASS = 32
DNS_MODES = {'off': 'off', 'automatic': 'opportunistic', 'hostname': 'hostname'}
# The host pattern of Android's own proxy validation (ProxyUtils): letters and
# digits, single hyphens inside a label, dots between labels. Anything Android
# would silently ignore is refused here with a message instead.
LABEL = r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*'
HOST_NAME = re.compile(rf'{LABEL}(?:\.{LABEL})*')
DNS_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
DNS_NAME = re.compile(rf'{DNS_LABEL}(?:\.{DNS_LABEL})*')
# What a value read back from Android may look like before it is shown.
READABLE = re.compile(r'[A-Za-z0-9*._-]{1,253}')

CALL_TIMEOUT = 4        # one `settings` call
WRITE_BUDGET = 9        # reading the old state and writing the new one
READ_BUDGET = 4         # the read-back, also after a failure
POLLS = 8               # how often to look for Android's own writes
POLL_DELAY = 0.25
SETTLE = 0.2            # Android writes the exclusion list right after the port
PARTLY = 'The network settings may be partly applied.'
SLOW = 'Android answers too slowly. Wait a moment, then retry.'
sleep = time.sleep


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def ipv4(value):
    parts = value.split('.')
    return len(parts) == 4 and all(
        part.isascii() and part.isdigit() and len(part) <= 3 and int(part) <= 255 and (part == '0' or part[0] != '0')
        for part in parts)


def check_name(value, what, example):
    """A DNS host name or an IPv4 address that is safe as one `settings` argument."""
    require(type(value) is str and value, f'Enter {what}.')
    require(len(value) <= MAX_NAME, f'{what.capitalize()} is too long. Use at most {MAX_NAME} characters.')
    require(':' not in value and '[' not in value,
            f'IPv6 addresses are not supported for {what}. Use a host name or an IPv4 address.')
    require(HOST_NAME.fullmatch(value) and all(len(label) <= 63 for label in value.split('.')),
            f'{what.capitalize()} must be a host name or an IPv4 address, for example {example}.')
    # A name that ends in a number is read as an address, so it must be a complete one.
    require(not value.rsplit('.', 1)[-1].isdigit() or ipv4(value),
            f'{what.capitalize()} is not a valid IPv4 address.')
    # `settings get` prints "null" for an unset key, so this name could not be read back.
    require(value.lower() != 'null', f'{what.capitalize()} cannot be "null".')
    return value


def check_port(value):
    require(type(value) is int and 1 <= value <= 65535, 'Proxy port must be a number from 1 to 65535.')
    return value


def check_bypass(value):
    require(type(value) is list and len(value) <= MAX_BYPASS,
            f'The bypass list can hold at most {MAX_BYPASS} entries.')
    for entry in value:
        require(type(entry) is str and entry, 'The bypass list has an empty entry.')
        check_name(entry[2:] if entry.startswith('*.') else entry, 'a bypass entry', 'example.com, *.example.com or 192.168.1.10')
        require(not (entry.startswith('*.') and ipv4(entry[2:])), 'A bypass wildcard needs a domain name, for example *.example.com.')
    require(len(set(map(str.lower, value))) == len(value), 'The bypass list has the same entry twice.')
    return list(value)


def check_dns(mode, hostname):
    require(type(mode) is str and mode in DNS_MODES, 'Private DNS must be off, automatic or a provider hostname.')
    require(type(hostname) is str, 'Invalid Private DNS hostname.')
    if mode != 'hostname':
        require(hostname == '', 'A Private DNS hostname needs the hostname mode.')
        return {'mode': mode, 'hostname': ''}
    require(hostname, 'Enter the hostname of the Private DNS provider.')
    require(len(hostname) <= MAX_NAME, f'The Private DNS hostname is too long. Use at most {MAX_NAME} characters.')
    require(DNS_NAME.fullmatch(hostname) and not hostname.rsplit('.', 1)[-1].isdigit() and hostname.lower() != 'null',
            'Private DNS needs a provider hostname such as dns.adguard.com, not an address.')
    return {'mode': mode, 'hostname': hostname}


def check_proxy(host, port, bypass):
    """None for "no proxy", which is spelled as an empty host with port 0 and no bypass entries."""
    require(type(host) is str, 'Invalid proxy host.')
    if host == '':
        require(type(port) is int and port == 0 and type(bypass) is list and not bypass,
                'Enter the proxy host, or turn the proxy off.')
        return None
    return {'host': check_name(host, 'the proxy host', 'proxy.example.com or 192.168.1.10'),
            'port': check_port(port), 'bypass': check_bypass(bypass)}


def wanted(params):
    """Validate the parameters of the `network_set` query (controller and worker both call this)."""
    require(isinstance(params, dict) and set(params) == {'proxy', 'dns'}, 'Invalid network settings.')
    proxy, dns = params['proxy'], params['dns']
    if proxy is not None:
        require(isinstance(proxy, dict) and set(proxy) == {'host', 'port', 'bypass'} and proxy['host'] != '',
                'Invalid proxy settings.')
        proxy = check_proxy(proxy['host'], proxy['port'], proxy['bypass'])
    require(isinstance(dns, dict) and set(dns) == {'mode', 'hostname'}, 'Invalid Private DNS settings.')
    return {'proxy': proxy, 'dns': check_dns(dns['mode'], dns['hostname'])}


def tool(run, seconds):
    """settings(*args) with one time budget for the whole series of calls."""
    deadline = time.monotonic() + seconds

    def settings(*args):
        remaining = deadline - time.monotonic()
        require(remaining > 0.05, SLOW)
        return run('settings', *args, timeout=min(CALL_TIMEOUT, remaining), limit=4096)
    return settings


def get(settings, key):
    """One global setting as text, or None when Android prints `null` (unset)."""
    text = settings('get', 'global', key).decode('utf-8', 'replace').strip()
    return None if text == 'null' else text


def read(settings):
    return {key: get(settings, key) for key in (LEGACY, HOST, PORT, EXCLUSION, DNS_MODE, DNS_SPECIFIER)}


def number(text):
    if not text or not text.isascii() or not text.isdigit() or len(text) > 5:
        return None
    value = int(text)
    return value if 1 <= value <= 65535 else None


def proxy_of(raw):
    """The proxy Android reports. A value Android itself would ignore counts as no proxy."""
    host, port, bypass = raw[HOST], number(raw[PORT]), []
    if host:
        bypass = [entry for entry in (part.strip() for part in (raw[EXCLUSION] or '').split(','))
                  if READABLE.fullmatch(entry)][:2 * MAX_BYPASS]
    elif raw[LEGACY]:
        # Written but not taken over by Android yet. Same parsing as ProxyTracker: the port defaults to 8080.
        parts = raw[LEGACY].split(':')
        host, port = parts[0], number(parts[1]) if len(parts) > 1 else 8080
    if not host or not READABLE.fullmatch(host) or '*' in host or port is None:
        return None
    return {'host': host, 'port': port, 'bypass': bypass}


def dns_of(raw):
    """An unset or unknown mode is Android's default, which is automatic."""
    mode = {'off': 'off', 'hostname': 'hostname'}.get(raw[DNS_MODE], 'automatic')
    specifier = raw[DNS_SPECIFIER] or ''
    return {'mode': mode, 'hostname': specifier if READABLE.fullmatch(specifier) and '*' not in specifier else ''}


def view(raw):
    return {'proxy': proxy_of(raw), 'dns': dns_of(raw)}


def same_dns(first, second):
    return first['mode'] == second['mode'] and (first['mode'] != 'hostname' or first['hostname'] == second['hostname'])


def seen(test):
    for attempt in range(POLLS):
        if attempt:
            sleep(POLL_DELAY)
        if test():
            return True
    return False


def clear_proxy(settings, raw):
    cleared = True
    if raw[HOST] or raw[LEGACY] not in (None, '', CLEAR):
        if raw[LEGACY] == CLEAR:
            # Writing the value that is already stored notifies nobody.
            settings('delete', 'global', LEGACY)
        settings('put', 'global', LEGACY, CLEAR)
        if raw[HOST]:
            cleared = seen(lambda: not get(settings, HOST))
    # Android has emptied the split keys. Remove them all, so no key can bring a proxy back at the next start.
    for key in (HOST, PORT, EXCLUSION, PAC, LEGACY):
        settings('delete', 'global', key)
    require(cleared, 'Android did not confirm that the proxy is off. Restart this device to be sure.')


def set_proxy(settings, proxy, raw):
    host, port, bypass = proxy['host'], proxy['port'], proxy['bypass']
    target = f'{host}:{port}'
    written = (raw[HOST], number(raw[PORT])) != (host, port)
    if written:
        if raw[LEGACY] == target:
            settings('delete', 'global', LEGACY)
        settings('put', 'global', LEGACY, target)
        require(seen(lambda: get(settings, HOST) == host and number(get(settings, PORT)) == port),
                'Android did not confirm the new proxy. Restart this device, then check the proxy again.')
        sleep(SETTLE)
    if bypass:
        settings('put', 'global', EXCLUSION, ','.join(bypass))
    elif raw[EXCLUSION] and not written:
        settings('delete', 'global', EXCLUSION)
    if written or raw[LEGACY] is not None:
        settings('delete', 'global', LEGACY)


def set_dns(settings, dns, raw):
    if dns['mode'] == 'hostname':
        if raw[DNS_SPECIFIER] != dns['hostname']:
            settings('put', 'global', DNS_SPECIFIER, dns['hostname'])
        if raw[DNS_MODE] != 'hostname':
            settings('put', 'global', DNS_MODE, 'hostname')
    else:
        # The stored hostname is kept, like Android's own settings screen does. It is not used in these modes.
        settings('put', 'global', DNS_MODE, DNS_MODES[dns['mode']])


def network_get(run, params, sink):
    require(params == {} and sink is None, 'Invalid network query.')
    return {**view(read(tool(run, READ_BUDGET))), 'error': None}


def network_set(run, params, sink):
    """Apply the wanted settings and return what Android reports afterwards.

    A failure in the middle is not raised: the result carries the real state
    plus `error`, so the caller can show both."""
    require(sink is None, 'Invalid network query.')
    want = wanted(params)
    settings = tool(run, WRITE_BUDGET)
    raw = read(settings)
    error = None
    try:
        if want['proxy'] is None:
            if any(raw[key] for key in (LEGACY, HOST, EXCLUSION)) or raw[PORT] not in (None, '', '0'):
                clear_proxy(settings, raw)
        elif want['proxy'] != proxy_of(raw) or raw[LEGACY] is not None:
            set_proxy(settings, want['proxy'], raw)
        if not same_dns(want['dns'], dns_of(raw)):
            set_dns(settings, want['dns'], raw)
    except RuntimeError as failure:
        error = f'{PARTLY} {failure}'
    try:
        now = view(read(tool(run, READ_BUDGET)))
    except RuntimeError as failure:
        raise RuntimeError(f'{error or PARTLY} The current settings could not be read: {failure}') from None
    if error is None and (now['proxy'] != want['proxy'] or not same_dns(now['dns'], want['dns'])):
        error = 'Android did not keep every setting. Check the values shown here, then apply again.'
    return {**now, 'error': error}


QUERIES = {'network_get': network_get, 'network_set': network_set}


def dispatch(store, request, uid, query):
    """Validate one request in the controller, then ask the running Android."""
    if request['op'] == 'network_get':
        result = query('network_get', {})
    else:
        result = query('network_set', wanted({
            'proxy': check_proxy(request['proxy_host'], request['proxy_port'], request['proxy_bypass']),
            'dns': {'mode': request['dns_mode'], 'hostname': request['dns_hostname']}}))
    require(isinstance(result, dict) and set(result) == {'proxy', 'dns', 'error'}, 'Device did not answer the network query')
    return {'runtime_id': request['id'], **result}
