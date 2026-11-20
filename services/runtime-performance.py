"""Live activity of a running device: process counters, the app in front, its frames.

Stateless on purpose. One call returns raw counters with the device clock they
were read at; the GUI turns two consecutive answers into CPU % and frames per
second. Every part can be missing: it is then None and `unavailable` says why.

See runtime-inspect.py for the contract of FIELDS, QUERIES and dispatch.
"""
import re
import time

FIELDS = {'performance_get': {'op', 'id'}}

# The only text that reaches a shell. It contains no request data. /proc has
# to be globbed, and one attach for all cheap parts keeps a poll short.
# `top` reports 0.0 %CPU inside the container, so the counters come from
# /proc/<pid>/stat. `read` keeps one line per process whatever its name holds.
SNAPSHOT = (
    'echo "uptime $(cat /proc/uptime)"; echo "ticks $(getconf CLK_TCK)"; '
    'echo "page $(getconf PAGESIZE)"; echo "cpus $(nproc)"; echo ANVIL_PERF_1; '
    "for f in /proc/[0-9]*/stat; do read -r l < $f && printf '%s\\n' \"$l\"; done 2>/dev/null; echo ANVIL_PERF_2; "
    "dumpsys -t 2 window 2>/dev/null | grep -E 'mCurrentFocus=|mFocusedApp=' | head -n 16; echo ANVIL_PERF_3; "
    'ps -A -o PID,UID,RSS,NAME 2>/dev/null; echo ANVIL_PERF_4; '
    'dumpsys -t 2 SurfaceFlinger --list 2>/dev/null | head -n 600; echo ANVIL_PERF_5')
SECTIONS = 5
BUDGET = 5.0                    # seconds of Android time for one poll, worst case
MAX_PROCESSES = 400
MAX_NAME = 128
MAX_LAYER = 256
MAX_LAYER_QUERIES = 3
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')
MAX_PACKAGE = 223
ACTIVITY = re.compile(r'[A-Za-z0-9_.$]{1,200}')
COMPONENT = re.compile(r' u\d+ ([A-Za-z][A-Za-z0-9_.]*)/([A-Za-z0-9_.$]+)')
NUMBER = re.compile(r'[0-9]{1,20}')
PS_ROW = re.compile(r'\s*([0-9]{1,10})\s+([0-9]{1,10})\s+([0-9]{1,12})\s+(\S.*)')
GFX_SECTION = re.compile(r'(?m)^\*\* Graphics info for pid ([0-9]{1,10}) \[([^\]\n]{1,256})\] \*\*[ \t]*$')
PENDING = 2 ** 62               # SurfaceFlinger marks unused or pending entries with 0 or INT64_MAX
SECOND = 1_000_000_000

NO_PROCESSES = 'Android did not list its processes.'
NO_FOCUS = 'No app is in front.'
NO_FOCUS_REPORT = 'Android did not report which app is in front.'
NO_FRAMES = 'This app does not report its frames.'
NO_FRAMES_ANSWER = 'Android did not answer the frame query in time.'
NO_SURFACE = 'This app has no surface to measure.'
NO_SURFACE_ANSWER = 'The Android display service did not answer.'
NO_TIME = 'There was no time left in this update.'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def printable(text, limit):
    return ''.join(c if c.isprintable() else '?' for c in text.strip())[:limit]


def sections(text):
    """Split the snapshot at its markers. A missing marker loses that part and the later ones."""
    parts = []
    for index in range(1, SECTIONS + 1):
        # A marker counts only as a whole line: process and window names are app text.
        head, found, text = ('\n' + text).partition(f'\nANVIL_PERF_{index}\n')
        parts.append(head[1:] if found else None)
    return parts


def parse_header(text):
    """Device clock in seconds, clock ticks per second, page size and CPU count."""
    values = {}
    for line in (text or '').splitlines():
        label, _, value = line.partition(' ')
        values.setdefault(label, value.split())
    try:
        uptime = float(values['uptime'][0])
    except (KeyError, IndexError, ValueError):
        return None
    if not 0 <= uptime < 1e10:      # also refuses nan and inf
        return None

    def whole(label, low, high, fallback):
        found = values.get(label) or ['']
        return int(found[0]) if NUMBER.fullmatch(found[0]) and low <= int(found[0]) <= high else fallback
    return {'uptime': uptime, 'ticks': whole('ticks', 1, 100000, 100), 'page': whole('page', 1024, 1 << 24, 4096),
            'cpus': whole('cpus', 1, 4096, None)}


def parse_stat(line):
    """One /proc/<pid>/stat line. The name sits in parentheses and may itself
    contain spaces and parentheses, so the fields are counted from the LAST ')'."""
    left, right = line.find(' ('), line.rfind(')')
    if left <= 0 or right < left + 2 or not NUMBER.fullmatch(line[:left]) or len(line[:left]) > 10:
        return None
    rest = line[right + 1:].split()
    # rest[0] is field 3 (state): utime 14, stime 15, starttime 22, vsize 23, rss 24.
    if len(rest) < 22 or not all(NUMBER.fullmatch(rest[index]) for index in (11, 12, 19, 20, 21)):
        return None
    return {'pid': int(line[:left]), 'comm': line[left + 2:right], 'ticks': int(rest[11]) + int(rest[12]),
            'start': int(rest[19]), 'vsize': int(rest[20]), 'pages': int(rest[21])}


def parse_ps(text):
    """`ps -A -o PID,UID,RSS,NAME` -> {pid: (uid, rss KiB, name)}."""
    rows = {}
    for line in (text or '').splitlines():
        match = PS_ROW.fullmatch(line)
        if match:
            rows[int(match[1])] = (int(match[2]), int(match[3]), printable(match[4], MAX_NAME))
    return rows


def parse_processes(stats, listing, page=4096):
    """Join the counters with names and memory. Without `ps` the short kernel
    name and the resident pages of the stat line are used and the owner is unknown."""
    if stats is None:
        return None
    names = parse_ps(listing)
    rows = []
    for line in stats.splitlines():
        entry = parse_stat(line)
        if entry is None or entry['vsize'] == 0:        # kernel threads have no address space
            continue
        uid, rss, name = names.get(entry['pid'], (None, entry['pages'] * page // 1024, ''))
        rows.append({'pid': entry['pid'], 'start': entry['start'], 'ticks': entry['ticks'], 'uid': uid,
                     'rss_kb': rss, 'name': name or printable(entry['comm'], MAX_NAME) or '?'})
    if not rows:
        return None
    rows.sort(key=lambda row: (-row['rss_kb'], row['pid']))
    return rows[:MAX_PROCESSES]


def valid_package(value):
    return isinstance(value, str) and len(value) <= MAX_PACKAGE and PACKAGE.fullmatch(value) is not None


def parse_focus(text):
    """The app in front from `dumpsys window`.

    mFocusedApp names the activity in front. mCurrentFocus names the window
    with input focus, which can be a popup, the notification shade or null.
    Its title is the full component, which is also how its layer is named."""
    focused, current = None, None
    for line in (text or '').splitlines():
        label, _, value = line.strip().partition('=')
        if label not in ('mFocusedApp', 'mCurrentFocus'):
            continue
        match = COMPONENT.search(value)
        if not match or not valid_package(match[1]) or not ACTIVITY.fullmatch(match[2]):
            continue
        package, activity = match[1], match[2]
        if activity.startswith('.'):
            activity = package + activity
        if label == 'mFocusedApp':
            focused = focused or (package, activity)
        else:
            current = current or (package, activity)
    chosen = focused or current
    if chosen is None:
        return None
    # The window title wins when it belongs to the same app: layers carry that exact name.
    if current and current[0] == chosen[0]:
        chosen = current
    return {'package': chosen[0], 'activity': chosen[1]}


def parse_gfxinfo(text, package, fallback_ms):
    """Frames drawn so far by the UI toolkit of `package`, with the clock they were read at.

    `since` changes when the counters restart, so the GUI never compares
    across a restart. Apps that draw by themselves have no such section."""
    found = None
    matches = list(GFX_SECTION.finditer(text))
    for index, match in enumerate(matches):
        name = match[2]
        if name != package and not name.startswith(package + ':'):
            continue
        body = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        total = re.search(r'(?m)^Total frames rendered: ([0-9]{1,18})[ \t]*$', body)
        if not total:
            continue
        since = re.search(r'(?m)^Stats since: ([0-9]{1,20})ns[ \t]*$', body)
        entry = {'pid': int(match[1]), 'total': int(total[1]), 'since': since[1] if since else ''}
        if name == package:
            found = entry
            break
        found = found or entry
    if found is None:
        return None
    uptime = re.search(r'(?m)^Uptime: ([0-9]{1,15}) Realtime: [0-9]+', text)
    return dict(found, uptime_ms=int(uptime[1]) if uptime else fallback_ms)


def layer_candidates(listing, package, activity=None):
    """Layers of `package` that can carry frames, best first.

    Every result is a verbatim line of `listing`. SurfaceView buffer layers
    (games, video) come first, then the window of the activity in front."""
    found = []
    for index, name in enumerate((listing or '').split('\n')):
        if not name or len(name) > MAX_LAYER or not name.isprintable() or not re.search(r'#[0-9]{1,10}$', name):
            continue
        if name.startswith(('SurfaceView[' + package + '/', 'SurfaceView - ' + package + '/')):
            score = 4 + 2 * ('(BLAST)' in name)
        elif name.startswith(package + '/'):
            score = 1 + bool(activity and name.startswith(package + '/' + activity + '#'))
        else:
            continue
        found.append((-score, -index, name))
    return [name for _, _, name in sorted(found)]


def parse_latency(text):
    """`dumpsys SurfaceFlinger --latency <layer>`: the refresh period in ns, then
    up to 127 rows of desired present, actual present and frame ready times."""
    lines = text.splitlines()
    period = int(lines[0]) if lines and NUMBER.fullmatch(lines[0].strip()) else 0
    refresh = round(SECOND / period, 1) if 1_000_000 <= period <= SECOND else None
    presents = []
    for line in lines[1:256]:
        fields = line.split()
        if len(fields) == 3 and all(NUMBER.fullmatch(field) for field in fields) and 0 < int(fields[1]) < PENDING:
            presents.append(int(fields[1]))
    if not presents:
        return {'refresh_hz': refresh, 'fps': None, 'newest': None, 'frames': 0} if refresh else None
    newest = max(presents)
    recent = [value for value in presents if newest - value < SECOND]
    span = newest - min(recent)
    # The rate of the last second in which this layer was presenting. `newest`
    # tells the GUI whether that second is new or left over from earlier.
    fps = (len(recent) - 1) * SECOND / span if len(recent) > 1 and span > 0 else float(len(recent))
    return {'refresh_hz': refresh, 'fps': round(fps, 1), 'newest': str(newest), 'frames': len(presents)}


def surface_kind(layer):
    return 'surfaceview' if layer.startswith('SurfaceView') else 'window'


def collect(run, params, sink):
    require(isinstance(params, dict) and not params and sink is None, 'Invalid activity request')
    deadline = time.monotonic() + BUDGET

    def left(most):
        value = min(most, deadline - time.monotonic())
        return value if value >= 0.3 else None

    text = run('sh', '-c', SNAPSHOT, timeout=4, limit=512 * 1024).decode('utf-8', 'replace')
    header, stats, focus_text, listing, layers = sections(text)
    clock = parse_header(header)
    require(clock is not None, 'Android did not report its clock. Retry in a moment.')
    unavailable = {}

    processes = parse_processes(stats, listing, clock['page'])
    if processes is None:
        unavailable['processes'] = NO_PROCESSES

    focus = parse_focus(focus_text)
    if focus is None:
        unavailable['focus'] = NO_FOCUS if focus_text and focus_text.strip() else NO_FOCUS_REPORT

    frames, surface = None, None
    if focus is None:
        unavailable['frames'] = unavailable['surface'] = unavailable['focus']
    else:
        package = focus['package']          # validated against PACKAGE by parse_focus
        timeout = left(2)
        if timeout is None:
            unavailable['frames'] = NO_TIME
        else:
            try:
                _, output = run('dumpsys', '-t', '2', 'gfxinfo', package, timeout=timeout, limit=512 * 1024, check=False)
                frames = parse_gfxinfo(output.decode('utf-8', 'replace'), package, int(clock['uptime'] * 1000))
                if frames is None:
                    unavailable['frames'] = NO_FRAMES
            except (RuntimeError, OSError):
                unavailable['frames'] = NO_FRAMES_ANSWER

        known = set((layers or '').split('\n'))
        candidates = layer_candidates(layers, package, focus['activity'])[:MAX_LAYER_QUERIES]
        answered = False
        for layer in candidates:
            timeout = left(1.5)
            if timeout is None:
                break
            # One argv item, never a shell, and only a name SurfaceFlinger listed in this very call.
            require(layer in known and 0 < len(layer) <= MAX_LAYER, 'Invalid layer')
            try:
                _, output = run('dumpsys', '-t', '1', 'SurfaceFlinger', '--latency', layer, timeout=timeout,
                                limit=32 * 1024, check=False)
            except (RuntimeError, OSError):
                continue
            answered = True
            entry = parse_latency(output.decode('utf-8', 'replace'))
            if entry is None:
                continue
            entry = dict(entry, layer=layer, kind=surface_kind(layer))
            # The layer that presented last is the one on screen now.
            if surface is None or int(entry['newest'] or 0) > int(surface['newest'] or 0):
                surface = entry
        if surface is None:
            unavailable['surface'] = (NO_SURFACE if not candidates else NO_SURFACE_ANSWER if not answered
                                      else NO_SURFACE)

    return {'uptime': clock['uptime'], 'ticks': clock['ticks'], 'cpus': clock['cpus'], 'processes': processes,
            'focus': focus, 'frames': frames, 'surface': surface, 'unavailable': unavailable}


QUERIES = {'performance': collect}


def dispatch(store, request, uid, query):
    require(request.get('op') == 'performance_get' and set(request) == FIELDS['performance_get'],
            'Unsupported device tool operation')
    report = query('performance', {})
    require(isinstance(report, dict) and isinstance(report.get('unavailable'), dict),
            'This device sent an invalid activity report. Stop and start it, then retry.')
    return dict(report, runtime_id=request['id'])
