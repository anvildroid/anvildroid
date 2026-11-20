"""Recent Android log lines of a running device (read only).

One op, `logs_get`, runs `logcat -d` with a fixed argv and returns parsed,
redacted lines plus a cursor. See runtime-inspect.py for the contract.

Format: `-v epoch,printable` gives an absolute timestamp and one prefixed
line per message line, so every line parses on its own.

Level: logcat applies the `*:<level>` filter itself. `-t <count>` is not used
because logd counts lines before that filter (`-t 4 '*:W'` can print nothing).
The newest part of the dump is read instead and the count is applied here.

Package: `logcat --pid` takes one process, an app can have several
(`package` and `package:name`). The processes are listed with `ps` and lines
are kept by process ID here. With exactly one process `--pid` is also passed,
so that a busy system does not push the app's lines out of the bounded read.

Cursor (`since`): `<last timestamp in ns>:<hashes of the raw lines that carry
that timestamp>`. Timestamps have millisecond precision and many lines share
one, and Android versions differ on whether `-T` includes the given instant.
So the next call asks from one millisecond earlier, drops everything older
than the cursor and drops the lines at the cursor's timestamp that were
already delivered. Nothing is lost and nothing is repeated.
"""
import collections
import hashlib
import importlib.util
import json
import time
from pathlib import Path
import re

MIN_LINES, MAX_LINES = 50, 2000
LEVELS = ('V', 'D', 'I', 'W', 'E', 'F')
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')
MAX_PACKAGE = 255
MAX_CURSOR_HASHES = 512
CURSOR = re.compile(r'(\d{1,20}):((?:[0-9a-f]{8}(?:,[0-9a-f]{8}){0,%d})?)' % (MAX_CURSOR_HASHES - 1))
READ_LIMIT = 640 * 1024         # newest bytes of the dump that are parsed
RESULT_LIMIT = 560 * 1024       # JSON size of the returned lines; a worker answers in one line of at most 1 MiB
MAX_TAG = 128
MAX_MESSAGE = 2000
CLEAN_BUDGET = 2.0              # seconds spent on redaction in one call; older lines are dropped after that
FUTURE = 60 * 10**9             # a line stamped later than this from now is not trusted as a position
BUFFERS = 'main,system,crash'   # crashes and native tombstones are logged to `crash`
PARAMS = {'lines', 'level', 'package', 'since'}
FIELDS = {'logs_get': {'op', 'id'} | PARAMS}
# "         1795077089.892  7099  7138 D BootAnimation: Playing files"
# Only the fixed prefix is matched by pattern. Tag and message are split by
# hand: an app chooses both, and a lazy pattern over them backtracks badly.
LINE = re.compile(r'\s*(\d{1,11})\.(\d{3,9})\s+(\d{1,10})\s+(\d{1,10})\s+([VDIWEF])\s(.*)')
PROCESS = re.compile(r'\s*(\d{1,10})\s+(\S+)\s*')


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


# Saved logs get shared: hide the same recognised secrets as diagnostic reports.
diagnostics = module('runtime-diagnostics')


clock = time.time_ns


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def parse_cursor(value):
    """Return (timestamp in ns, Counter of line hashes at that timestamp)."""
    match = CURSOR.fullmatch(value)
    require(match, 'Invalid log position. Clear the log view and retry.')
    return int(match[1]), collections.Counter(match[2].split(',') if match[2] else ())


def check(params):
    require(isinstance(params, dict) and set(params) == PARAMS, 'Invalid log request')
    lines, level, package, since = (params[key] for key in ('lines', 'level', 'package', 'since'))
    require(type(lines) is int and MIN_LINES <= lines <= MAX_LINES,
            f'The number of log lines must be between {MIN_LINES} and {MAX_LINES}.')
    require(type(level) is str and level in LEVELS, 'Invalid log level')
    require(package is None or (type(package) is str and len(package) <= MAX_PACKAGE and PACKAGE.fullmatch(package)),
            'Enter a full app package name, for example com.example.app.')
    require(since is None or (type(since) is str and len(since) <= 21 + 9 * MAX_CURSOR_HASHES),
            'Invalid log position. Clear the log view and retry.')
    return lines, level, package, None if since is None else parse_cursor(since)


def processes(run, package):
    """Process IDs of every process of one app: `package` and `package:name`."""
    found = set()
    for row in run('ps', '-A', '-o', 'PID,NAME', timeout=5).decode(errors='replace').splitlines():
        match = PROCESS.fullmatch(row)
        if match and (match[2] == package or match[2].startswith(package + ':')):
            found.add(int(match[1]))
    return found


def start_argument(moment):
    """`-T` value one millisecond before the cursor, as seconds.milliseconds."""
    start = max(0, moment - 1_000_000)
    return f'{start // 10**9}.{start % 10**9 // 10**6:03d}'


def parse(raw, cut=False):
    """Return ([(ns, hash, pid, tid, level, tag, message)], unparsed line count)."""
    rows = raw.split(b'\n')
    if cut and rows:
        rows = rows[1:]          # a bounded tail usually starts inside a line
    entries, unparsed = [], 0
    for row in rows:
        row = row.rstrip(b'\r')
        if not row.strip() or row.startswith(b'--------- '):   # "--------- beginning of main"
            continue
        match = LINE.fullmatch(row.decode(errors='replace'))
        if not match:
            unparsed += 1
            continue
        tag, separator, message = match[6].partition(': ')
        if not separator:
            if not tag.endswith(':'):
                unparsed += 1
                continue
            tag = tag[:-1]
        moment = int(match[1]) * 10**9 + int(match[2].ljust(9, '0'))
        entries.append((moment, hashlib.sha256(row).hexdigest()[:8], int(match[3]), int(match[4]), match[5],
                        tag.rstrip(), message))
    return entries, unparsed


def clean(value, limit):
    # redact_text also removes control characters; it can decode an escaped
    # line break, and one log line must stay one line.
    # Cut first: redaction cost grows with the length and an app chooses it.
    cut = len(value) > limit
    value = re.sub(r'[\r\n]+', ' ', diagnostics.redact_text(value[:limit]))
    return value[:limit] + ' …' if cut or len(value) > limit else value


def logs(run, params, sink):
    require(sink is None, 'Unexpected file descriptor')
    lines, level, package, since = check(params)
    cursor = params['since']
    report = {'lines': [], 'cursor': cursor, 'truncated': False, 'unparsed': 0, 'note': None}
    pids = None
    if package is not None:
        pids = processes(run, package)
        if not pids:
            report['note'] = f'{package} is not running. Open the app to see its log lines.'
            return report
    argv = ['-d', '-b', BUFFERS, '-v', 'epoch,printable']
    if pids is not None and len(pids) == 1:
        argv.append(f'--pid={min(pids)}')
    if since is not None:
        argv += ['-T', start_argument(since[0])]
    raw = run('logcat', *argv, f'*:{level}', timeout=8, limit=READ_LIMIT, tail=True)
    cut = len(raw) >= READ_LIMIT
    entries, report['unparsed'] = parse(raw, cut)
    truncated = cut
    # An app can write a line with any timestamp. One from the far future would
    # become the position and hide every later line.
    latest = clock() + FUTURE
    trusted = [entry for entry in entries if entry[0] <= latest]
    report['unparsed'] += len(entries) - len(trusted)
    entries = trusted
    if since is not None:
        moment, seen = since
        entries = [entry for entry in entries if entry[0] >= moment]
    if entries:
        newest = max(entry[0] for entry in entries)
        report['cursor'] = f'{newest}:' + ','.join([entry[1] for entry in entries if entry[0] == newest][-MAX_CURSOR_HASHES:])
    if since is not None:
        fresh = []
        for entry in entries:
            if entry[0] == moment and seen[entry[1]] > 0:
                seen[entry[1]] -= 1          # delivered by an earlier call
            else:
                fresh.append(entry)
        entries = fresh
    if pids is not None:
        entries = [entry for entry in entries if entry[2] in pids]
    if len(entries) > lines:
        entries, truncated = entries[-lines:], True
    kept, size, deadline = [], 0, time.monotonic() + CLEAN_BUDGET
    for moment, _, pid, tid, priority, tag, message in reversed(entries):
        if time.monotonic() > deadline:
            truncated = True
            break
        item = {'time': moment // 10**6, 'pid': pid, 'tid': tid, 'level': priority,
                'tag': clean(tag, MAX_TAG), 'message': clean(message, MAX_MESSAGE)}
        size += len(json.dumps(item)) + 2
        if size > RESULT_LIMIT:
            truncated = True
            break
        kept.append(item)
    report['lines'] = kept[::-1]
    report['truncated'] = truncated
    return report


QUERIES = {'logs': logs}


def dispatch(store, request, uid, query):
    require(request.get('op') == 'logs_get' and set(request) == FIELDS['logs_get'], 'Unsupported device tool operation')
    params = {key: request[key] for key in sorted(PARAMS)}
    check(params)
    report = query('logs', params)
    require(isinstance(report, dict) and isinstance(report.get('lines'), list), 'Device returned an invalid log answer')
    return {'runtime_id': request['id'], **report}
