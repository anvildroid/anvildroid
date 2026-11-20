"""Screenshot of a running device's whole Android display.

The caller opens the destination file and passes its descriptor; the
controller never receives or opens a path. Android writes the PNG into an
anonymous controller-owned memory file. Only after that file was checked is it
copied to the caller's descriptor, by a short-lived helper process that is
killed at a deadline: a user-chosen file can sit on a filesystem that stalls
forever (FUSE, a network mount), and even fstat() can hang there.

See runtime-inspect.py for the feature contract.
"""
import fcntl
import os
import pwd
import struct
import subprocess
import sys
import zlib

FIELDS = {'screenshot_save': {'op', 'id'}}
DESCRIPTOR_OPS = {'screenshot_save'}

MAX_PNG = 48 * 1024**2
MIN_PNG = 128                   # signature, IHDR, one IDAT and IEND already need 57 bytes plus pixels
MAX_SIDE = 16384
CAPTURE_TIMEOUT = 15
PROBE_DEADLINE = 5
COPY_DEADLINE = 10
WIPE_DEADLINE = 3
SIGNATURE = b'\x89PNG\r\n\x1a\n'
IHDR = b'\x00\x00\x00\rIHDR'
IEND = b'\x00\x00\x00\x00IEND\xaeB`\x82'
INVALID = 'Android returned an invalid screenshot. Try again.'
NOT_A_FILE = 'The screenshot destination must be a regular file. Choose another file name.'
BUSY = 'A screenshot of this device is still being saved. Try again in a moment.'

# Runs as its own process with the screenshot on stdin and the caller's file on
# stdout, so the controller can kill it when the destination stops answering.
# It contains no request data; the mode and byte count are separate arguments.
HELPER = r'''
import os, stat, sys
mode, size, user, group = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
if os.geteuid() == 0 and user != 0:
    # Touch the caller's file as the caller: root would bypass quotas and the
    # write-time checks some special files make.
    os.setgroups([])
    os.setgid(group)
    os.setuid(user)
if mode == 'probe':
    sys.exit(0 if stat.S_ISREG(os.fstat(1).st_mode) else 3)
if mode == 'wipe':
    os.ftruncate(1, 0)
    sys.exit(0)
try:
    os.ftruncate(1, 0)
    os.lseek(1, 0, os.SEEK_SET)
    offset = 0
    while offset < size:
        part = os.pread(0, min(1 << 20, size - offset), offset)
        if not part:
            raise OSError(5, 'The screenshot buffer ended early')
        view = memoryview(part)
        while view:
            view = view[os.write(1, view):]
        offset += len(part)
except OSError as error:
    try:
        os.ftruncate(1, 0)
    except OSError:
        pass
    sys.stderr.write(error.strerror or str(error))
    sys.exit(1)
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def png_size(head, tail, size):
    """Return (width, height) of a complete PNG, given its first 33 bytes, its
    last 12 bytes and its length. Raises for anything else."""
    require(type(size) is int and MIN_PNG <= size <= MAX_PNG, INVALID)
    require(isinstance(head, bytes) and len(head) == 33 and head[:8] == SIGNATURE and head[8:16] == IHDR, INVALID)
    width, height = struct.unpack('>II', head[16:24])
    require(0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE, INVALID)
    require(zlib.crc32(head[12:29]) == struct.unpack('>I', head[29:33])[0], INVALID)
    # A capture that was cut short has no end chunk.
    require(tail == IEND, INVALID)
    return width, height


def screencap(run, params, sink):
    require(params == {}, 'Invalid screenshot parameters')
    require(isinstance(sink, int) and not isinstance(sink, bool), 'Screenshot needs a controller buffer')
    written = run('screencap', '-p', sink=sink, limit=MAX_PNG, timeout=CAPTURE_TIMEOUT)
    require(type(written) is int and written > 0, 'Android returned an empty screenshot. Try again.')
    return {'bytes': written}


QUERIES = {'screencap': screencap}


def writable(descriptor):
    """True when the descriptor was opened for writing. F_GETFL never asks the
    filesystem, so it is safe under the controller's lock. O_PATH reads as read-only."""
    try:
        return fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR)
    except OSError:
        return False


def helper(mode, source, descriptor, size, deadline, command=None, owner=None):
    """Run one helper step on the caller's descriptor. Returns (exit code, error
    text), or (None, '') when it had to be killed at the deadline."""
    user, group = owner or (os.geteuid(), os.getegid())
    argv = command or [sys.executable, '-I', '-S', '-c', HELPER, mode, str(size), str(user), str(group)]
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL if source is None else source, stdout=descriptor,
                               stderr=subprocess.PIPE, env={}, close_fds=True)
    try:
        _, errors = process.communicate(timeout=deadline)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # Stuck in a write that ignores signals: never wait for it.
            process.stderr.close()
        return None, ''
    return process.returncode, errors.decode(errors='replace').strip()[-200:]


def probe(descriptor, deadline=None, command=None, owner=None):
    """Refuse directories, FIFOs, sockets and devices. fstat() runs in the helper
    because it can hang on a stalled filesystem."""
    code, _ = helper('probe', None, descriptor, 0, PROBE_DEADLINE if deadline is None else deadline, command, owner)
    require(code is not None, 'The destination folder is not responding. Choose another folder.')
    require(code == 0, NOT_A_FILE)


def deliver(buffer, descriptor, size, deadline=None, command=None, owner=None):
    """Copy `size` bytes of the checked buffer to the caller's file. A failed or
    stalled copy leaves an empty file, never a partial PNG that looks valid."""
    code, detail = helper('copy', buffer, descriptor, size, COPY_DEADLINE if deadline is None else deadline, command, owner)
    if code == 0:
        return
    helper('wipe', None, descriptor, 0, WIPE_DEADLINE, owner=owner)
    if code is None:
        raise RuntimeError('Writing the screenshot took too long, so it was not saved. Choose another folder.')
    raise RuntimeError(f'Could not write the screenshot: {detail or "the copy failed"}. '
                       'Check the free space or choose another folder.')


def dispatch(store, request, uid, query, descriptor):
    require(request['op'] == 'screenshot_save', 'Unsupported device tool operation')
    require(isinstance(descriptor, int) and not isinstance(descriptor, bool) and descriptor >= 0
            and writable(descriptor),
            'The screenshot destination must be a file opened for writing.')
    key = 'screenshot:' + request['id']
    with store.reserve(key, BUSY):
        # The account lookup can be slow (LDAP), so it runs without the lock.
        try:
            owner = (uid, pwd.getpwuid(uid).pw_gid)
        except KeyError:
            owner = (uid, 65534)
        probe(descriptor, owner=owner)
    # Android never sees the caller's file, only this controller-owned memory.
    buffer = os.memfd_create('anvildroid-screenshot', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        result = query('screencap', {}, buffer)
        # The worker held a copy of this descriptor: freeze the content before checking it.
        fcntl.fcntl(buffer, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
        size = os.fstat(buffer).st_size
        require(isinstance(result, dict) and type(result.get('bytes')) is int and result['bytes'] == size, INVALID)
        require(MIN_PNG <= size <= MAX_PNG, INVALID)
        width, height = png_size(os.pread(buffer, 33, 0), os.pread(buffer, 12, size - 12), size)
        with store.reserve(key, BUSY):
            deliver(buffer, descriptor, size, owner=owner)
    finally:
        os.close(buffer)
    return {'runtime_id': request['id'], 'bytes': size, 'width': width, 'height': height}
