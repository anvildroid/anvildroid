"""Back up and restore one device's Android user data (root side).

Archive: GNU tar (pax, numeric owners, modes, links, xattrs, sparse files)
compressed in-process with zlib level 1 into <backup id>.tar.gz. It lives in a
private root-owned folder: inside the caller's data folder (Settings > Storage)
when one is set, otherwise <store>/backups. The registry (backups.json in the
controller store) records each archive's SHA-256, sizes and the Android image
it was made with. Only an archive that still matches its recorded digest is
ever unpacked.

Restore unpacks into a private staging directory on the device's own
filesystem and then swaps it with the live data directory in one atomic
renameat2(RENAME_EXCHANGE); the previous data is removed only afterwards, so a
failed or cancelled restore leaves the device exactly as it was.

No request carries a path: archives are addressed by backup ID, the data
directory comes from the registry (managed) or the pinned System Waydroid
record (default), and tar reaches both through inherited descriptors.
"""
import contextlib
import copy
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zlib


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


storage = module('runtime-storage')

FIELDS = {
    'backups_get': {'op', 'id'},
    'backup_create': {'op', 'id'},
    'backup_restore': {'op', 'id', 'backup_id', 'allow_image_mismatch'},
    'backup_delete': {'op', 'id', 'backup_id'},
    'backup_cancel': {'op', 'id'},
}
RUNTIME = r'r-[0-9a-f]{32}'
BACKUP = r'[0-9a-f]{32}'
PREFIX = '.anvildroid-backups-'
FOLDER_NAME = re.escape(PREFIX) + r'[0-9a-f]{8}'
SUFFIX = '.tar.gz'
PARTIAL = '.partial'
MANAGED_STAGING = 'restore-data'
DEFAULT_STAGING = '.anvildroid-restore-'
MARGIN = 64 * 1024**2
# The controller store usually sits on the root filesystem: never fill it.
INTERNAL_RESERVE = 1024**3
MAX_PER_DEVICE = 32
MAX_PER_USER = 512
MAX_TOTAL = 4096
# Bytes of tar stream allowed per entry on top of file contents (headers, xattr records, padding).
ENTRY_OVERHEAD = 2560
SPACE_CHECK_BYTES = 64 * 1024**2
UNKNOWN_LIMIT = 6
CHUNK = 1024 * 1024
STALL_SECONDS = 900
CHECK_SECONDS = 10
# The GUI recognises this sentence to offer "restore anyway".
IMAGE_MISMATCH = 'This backup was made with a different Android image.'


class Cancelled(Exception):
    pass


def gib(value):
    return f'{value / 1024**3:.2f} GiB'


def private_identity(fd):
    """A directory only the controller can read or change (root in production)."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError('Backup storage must remain a private root-owned directory')
    return {'device': info.st_dev, 'inode': info.st_ino, 'fsid': os.fstatvfs(fd).f_fsid}


def node(info):
    return {'device': info.st_dev, 'inode': info.st_ino}


def free_bytes(fd):
    usage = os.fstatvfs(fd)
    return usage.f_bavail * usage.f_frsize


def read_state(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 4 * 1024**2):
            raise RuntimeError('Unsafe backup metadata file')
        return json.load(stream)


def read_json(path):
    """Small controller-written metadata inside a private instance directory."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 1024**2:
            raise RuntimeError('Unsafe device metadata file')
        return json.load(stream)


def exchange(first_fd, first, second_fd, second):
    """Atomically swap two directory entries (renameat2 RENAME_EXCHANGE)."""
    function = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if function is None:
        raise OSError(errno.ENOSYS, 'renameat2 is unavailable')
    if function(first_fd, os.fsencode(first), second_fd, os.fsencode(second), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def measure(fd, cancelled=lambda: False):
    """Bytes tar will read and the entry count, through pinned descriptors only.

    Device files are refused: Android user data has none, and a restore must
    never create one."""
    seen = set()
    totals = [0, 0]

    def walk(directory):
        with os.scandir(directory) as iterator:
            names = [entry.name for entry in iterator]
        for name in names:
            if cancelled(): raise Cancelled()
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            totals[1] += 1
            if stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode):
                raise RuntimeError('Android data contains a device file, which cannot be backed up or restored safely')
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try: walk(child)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    if key in seen: continue
                    seen.add(key)
                totals[0] += info.st_size
    walk(fd)
    return totals[0], totals[1]


def remove_named(parent_fd, name, identity):
    """Delete a directory only while its name still refers to the known inode."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        if node(os.fstat(fd)) != identity: return
        # Contents go through the verified descriptor; the name is only used
        # for the final rmdir, which cannot remove anything but an empty directory.
        storage.clear_directory(fd)
        if node(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) == identity:
            os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(fd)


def reason(failure):
    """Plain words for an error; never a bare errno."""
    if isinstance(failure, OSError) and failure.strerror:
        return failure.strerror[0].lower() + failure.strerror[1:]
    return str(failure) or type(failure).__name__


def clear_managed_staging(instance):
    """Remove what an interrupted restore left in a device: always the unwanted copy."""
    parent = storage.open_directory(instance)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022: raise RuntimeError('Unsafe device directory')
        try: found = os.stat(MANAGED_STAGING, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError: return
        if not stat.S_ISDIR(found.st_mode): raise RuntimeError('Unsafe restore staging directory')
        shutil.rmtree(MANAGED_STAGING, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def waydroid_state(existing):
    """STOPPED/RUNNING/FROZEN, or None when lxc-info gave no answer (it has a 1 s limit)."""
    for attempt in range(3):
        try: return existing.state()
        except (OSError, RuntimeError):
            if attempt < 2: time.sleep(0.5)
    return None


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class Control:
    """Cancellation for one job; refuses to cancel once the swap has begun."""
    def __init__(self):
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.process = None
        self.final = False
        # Disk space this job still expects to use, so parallel jobs do not count it twice.
        self.device, self.budget, self.written = None, 0, 0

    def kill(self):
        if self.process is not None and self.process.poll() is None:
            try: os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def cancel(self):
        with self.lock:
            if self.final: return False
            self.event.set()
            self.kill()
            return True

    def attach(self, process):
        with self.lock:
            self.process = process
            if self.event.is_set(): self.kill()

    def detach(self):
        with self.lock:
            self.kill()
            process, self.process = self.process, None
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try: stream.close()
                    except OSError: pass
            process.wait()

    def commit(self):
        with self.lock:
            if self.event.is_set(): raise Cancelled()
            self.final = True

    def cancelled(self):
        return self.event.is_set()


def tail(stream):
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - 2048))
    return stream.read().decode(errors='replace').strip()[-600:] or 'tar returned an error'


def complaints(stream):
    """tar exits 0 after warnings such as an extended attribute it could not copy."""
    stream.seek(0)
    return stream.read(65536).decode(errors='replace').strip()[-600:]


_tar = []


def require_gnu_tar():
    if _tar: return _tar[0]  # checked once per controller run, not on every request
    path = shutil.which('tar')
    if path is None:
        raise RuntimeError('Backups need GNU tar, which is not installed')
    result = subprocess.run([path, '--version'], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    if result.returncode or b'GNU tar' not in result.stdout:
        raise RuntimeError('Backups need GNU tar; another tar is installed')
    _tar.append(path)
    return path


def create_archive(source, output, job, control, check, limit=None, floor=0):
    """tar the pinned data directory into `output`; returns (sha256, archive bytes, data bytes).

    `limit` is the stream size measured beforehand and `floor` the free space
    that must remain: data that grows meanwhile cannot fill the disk."""
    argv = [require_gnu_tar(), '--create', '--file=-', '--format=posix', '--numeric-owner', '--xattrs',
            '--xattrs-include=*', '--sparse', '--one-file-system', '--warning=no-file-ignored',
            f'--directory=/proc/self/fd/{source}', '.']
    digest = hashlib.sha256()
    packer = zlib.compressobj(1, zlib.DEFLATED, 31)
    archive_bytes = data_bytes = spaced = 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                   pass_fds=(source,), start_new_session=True)
        control.attach(process)
        try:
            pipe = process.stdout.fileno()
            active = checked = time.monotonic()
            while True:
                if control.cancelled(): raise Cancelled()
                ready, _, _ = select.select([pipe], [], [], 2)
                now = time.monotonic()
                if now - checked >= CHECK_SECONDS:
                    check()
                    checked = now
                if not ready:
                    if now - active > STALL_SECONDS: raise RuntimeError('Backup stalled while reading Android data')
                    continue
                chunk = os.read(pipe, CHUNK)
                if not chunk: break
                active = now
                data_bytes += len(chunk)
                if limit is not None and data_bytes > limit:
                    raise RuntimeError('Android data grew while it was being backed up, so the backup was abandoned. '
                                       'Keep the device stopped and back up again.')
                packed = packer.compress(chunk)
                write_all(output, packed)
                digest.update(packed)
                archive_bytes += len(packed)
                control.written = archive_bytes
                if archive_bytes - spaced >= SPACE_CHECK_BYTES:
                    spaced = archive_bytes
                    if free_bytes(output) < floor:
                        raise RuntimeError('The disk is filling up, so the backup was abandoned to keep the reserved free space.')
                job['done_bytes'] = data_bytes
            packed = packer.flush()
            write_all(output, packed)
            digest.update(packed)
            archive_bytes += len(packed)
            code = process.wait(timeout=120)
            if control.cancelled(): raise Cancelled()
            if code: raise RuntimeError('Backup failed: ' + tail(errors))
            # A backup that silently lacks something is worse than none.
            noise = complaints(errors)
            if noise: raise RuntimeError('Backup abandoned because tar could not copy everything: ' + noise)
        finally:
            control.detach()
    return digest.hexdigest(), archive_bytes, data_bytes


def verify_archive(archive, entry, job, control):
    """The archive must be byte-identical to what this controller wrote."""
    info = os.fstat(archive)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise RuntimeError('Unsafe backup archive')
    digest = hashlib.sha256()
    done = 0
    os.lseek(archive, 0, os.SEEK_SET)
    while True:
        if control.cancelled(): raise Cancelled()
        chunk = os.read(archive, CHUNK)
        if not chunk: break
        digest.update(chunk)
        done += len(chunk)
        job['done_bytes'] = done
    if done != entry['archive_bytes'] or digest.hexdigest() != entry['sha256']:
        raise RuntimeError('This backup is damaged or was changed after it was made, so it cannot be restored. Nothing was changed.')


def feed(process, data, control, clock):
    """Write to tar without blocking forever: cancel and the stall limit still apply."""
    pipe = process.stdin.fileno()
    view = memoryview(data)
    while view:
        if control.cancelled(): raise Cancelled()
        _, ready, _ = select.select([], [pipe], [], 2)
        if not ready:
            if time.monotonic() - clock[0] > STALL_SECONDS:
                raise RuntimeError('Restore stalled while writing Android data. Nothing was changed.')
            continue
        try: sent = os.write(pipe, view[:65536])
        except BlockingIOError: continue
        view = view[sent:]
        clock[0] = time.monotonic()


def extract_archive(archive, staging, entry, job, control, check):
    """Unpack a verified archive into the pinned, empty staging directory."""
    argv = [require_gnu_tar(), '--extract', '--file=-', '--numeric-owner', '--same-owner', '--same-permissions',
            '--xattrs', '--xattrs-include=*', '--warning=no-timestamp', f'--directory=/proc/self/fd/{staging}']
    unpacker = zlib.decompressobj(31)
    limit = entry['data_bytes']
    done = 0
    os.lseek(archive, 0, os.SEEK_SET)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=errors, stderr=errors,
                                   pass_fds=(staging,), start_new_session=True)
        os.set_blocking(process.stdin.fileno(), False)
        control.attach(process)
        try:
            checked = time.monotonic()
            clock = [checked]
            try:
                while True:
                    if control.cancelled(): raise Cancelled()
                    if time.monotonic() - checked >= CHECK_SECONDS:
                        check()
                        checked = time.monotonic()
                    chunk = os.read(archive, CHUNK)
                    if not chunk: break
                    while chunk:
                        data = unpacker.decompress(chunk, 4 * CHUNK)
                        chunk = unpacker.unconsumed_tail
                        done += len(data)
                        if done > limit: raise RuntimeError('Backup archive is larger than recorded; nothing was changed')
                        feed(process, data, control, clock)
                        control.written = done
                        job['done_bytes'] = done
                if not unpacker.eof or unpacker.unused_data or done != limit:
                    raise RuntimeError('Backup archive is incomplete; nothing was changed')
                process.stdin.close()
            except BrokenPipeError:
                process.wait(timeout=120)
                if control.cancelled(): raise Cancelled()
                raise RuntimeError('Restore failed: ' + tail(errors))
            deadline = time.monotonic() + 3600
            while True:
                try:
                    code = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if control.cancelled(): raise Cancelled() from None
                    if time.monotonic() > deadline:
                        raise RuntimeError('Restore stalled while finishing. Nothing was changed.') from None
            if control.cancelled(): raise Cancelled()
            if code: raise RuntimeError('Restore failed: ' + tail(errors))
            # tar exits 0 when it could not set an extended attribute or owner: that is not a faithful restore.
            noise = complaints(errors)
            if noise:
                raise RuntimeError('The backup could not be reproduced exactly, so it was not applied. Nothing was changed. ' + noise)
        finally:
            control.detach()


class ManagedTarget:
    """Android data of an AnvilDroid device: instances/<id>/android/<id>/data."""
    def __init__(self, store, record):
        self.store = store
        self.record = record
        self.id = record['id']
        self.uid = record['owner_uid']
        self.name = record['name']
        self.instance = store.backend.location(self.id)
        self.container = self.instance / 'android' / self.id

    def blocked(self):
        store = self.store
        if (self.id in store.active_jobs or store.storage.busy is not None
                or store.app_jobs.get(self.id, {}).get('status') == 'Running'):
            return 'Wait for the current device operation to finish.'
        if len(store.active_jobs) >= 4:
            return 'Too many device operations are running. Try again when one finishes.'
        if self.record['state'] not in ('Prepared', 'Stopped', 'Error'):
            return 'Stop this device before backing up or restoring its data.'
        if store.imports.get(self.id) == 'pending' or (self.instance / 'reinstall.json').exists():
            return 'Finish setting up this device first.'
        store.storage.ensure(self.id)
        if not self.instance.is_dir() or not store.android.quiescent(self.id):
            return 'Android is still shutting down. Try again in a moment.'
        return None

    @contextlib.contextmanager
    def hold(self):
        # The worker takes this lock to run Android: holding it keeps the data quiescent.
        lock = os.open(self.instance / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Android is still running on this device') from None
            storage.assert_no_child_mounts(self.instance)
            yield
        finally:
            os.close(lock)

    def check(self, final=False):
        pass  # hold() covers the whole job

    def removable(self):
        return True

    def recover(self, manager):
        # The swap is atomic, so anything still in the staging name is the unwanted copy.
        clear_managed_staging(self.instance)

    def has_data(self):
        try:
            with os.scandir(self.container / 'data') as entries: return any(entries)
        except OSError:
            return False

    def source(self, required=True):
        try:
            return storage.open_directory(self.container / 'data')
        except FileNotFoundError:
            if required: raise RuntimeError('This device has no Android data yet. Start it once, then back up.') from None
            return None

    def image(self, manager, compute=True):
        try:
            images = read_json(self.instance / 'prepared/manifest.json')['images']
            identity = {'system': images['system.img'], 'vendor': images['vendor.img']}
            if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in identity.values()):
                identity = None
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            identity = None
        try:
            sdk = read_json(self.instance / 'support/compatibility.json').get('sdk')
        except (OSError, ValueError, RuntimeError, AttributeError):
            sdk = None
        selection = self.store.selections.get(self.id) or {}
        label = (selection.get('custom_name') or ('Waydroid ' + selection['flavor'] if selection.get('flavor') in ('VANILLA', 'GAPPS')
                 else 'Custom image' if selection.get('flavor') == 'CUSTOM' else 'Installed Waydroid image'))
        return identity, sdk if type(sdk) is int else None, str(label)[:128]

    def staging(self, manager, control):
        clear_managed_staging(self.instance)
        parent = storage.open_directory(self.instance)
        try:
            os.mkdir(MANAGED_STAGING, 0o700, dir_fd=parent)
            return parent, MANAGED_STAGING
        except Exception:
            os.close(parent)
            raise

    def data_parent(self, staging_parent):
        self.container.mkdir(parents=True, exist_ok=True)
        return storage.open_directory(self.container), 'data'

    def committed(self, manager, staging_fd):
        pass

    def settled(self, manager):
        pass


class DefaultTarget:
    """System Waydroid: the user's own ~/.local/share/waydroid/data, pinned at Enable management."""
    id = 'default'

    def __init__(self, store, uid, saved):
        self.store = store
        self.existing = store.existing
        self.uid = uid
        self.saved = saved
        self.name = saved.get('name', 'System Waydroid')
        self.unknown = 0

    def blocked(self):
        store = self.store
        if 'default' in store.active_jobs or any(store.imports.get(job) == 'pending' for job in store.active_jobs):
            return 'Wait for the current System Waydroid operation to finish.'
        if len(store.active_jobs) >= 4:
            return 'Too many device operations are running. Try again when one finishes.'
        if not self.saved.get('data_identity') or not self.saved.get('data_path'):
            return 'Start System Waydroid and choose Enable management once, so its Android data folder is known.'
        if self.existing.state() != 'STOPPED':
            return 'Stop System Waydroid before backing up or restoring its data.'
        return None

    @contextlib.contextmanager
    def hold(self):
        yield  # Waydroid has no lock we can take; check() re-verifies that it stays stopped.

    def check(self, final=False):
        state = waydroid_state(self.existing)
        if state is None:
            # One slow answer under this job's own disk load must not abort hours of work,
            # but "no answer" is never taken for "stopped" where it matters.
            self.unknown += 1
            if final or self.unknown >= UNKNOWN_LIMIT:
                raise RuntimeError('Could not confirm that System Waydroid is still stopped, so the operation was abandoned. Try again.')
            return
        self.unknown = 0
        if state != 'STOPPED':
            raise RuntimeError('System Waydroid was started during the operation. Keep it stopped and try again.')

    def removable(self):
        # A Waydroid started in the last moment may have mounted the previous data.
        return waydroid_state(self.existing) == 'STOPPED'

    def recover(self, manager):
        manager.recover_default(self.existing)
        if manager.pending:
            raise RuntimeError('An earlier restore is not finished: its previous data is still in use. '
                               'Stop System Waydroid completely, then try again.')
        self.saved = self.existing.saved()

    def has_data(self):
        return bool(self.saved.get('data_path'))

    def source(self, required=True):
        return self.existing.import_source(self.uid)

    def image(self, manager, compute=True):
        return manager.default_image(self.existing, compute), None, 'System Waydroid image'

    def data_name(self):
        path = Path(self.saved['data_path'])
        if not path.is_absolute() or path == Path('/') or '..' in path.parts:
            raise RuntimeError('System Waydroid data folder is unavailable')
        return path

    def staging(self, manager, control):
        path = self.data_name()
        parent = storage.open_directory(path.parent)
        try:
            name = DEFAULT_STAGING + uuid.uuid4().hex[:8]
            os.mkdir(name, 0o700, dir_fd=parent)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                # The parent belongs to the user: only a directory we just made counts.
                new = node(os.fstat(fd))
                private_identity(fd)
                if os.listdir(fd): raise RuntimeError('Restore staging directory was changed')
            finally:
                os.close(fd)
            old = node(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
            manager.journal({'parent': str(path.parent), 'staging': name, 'new': new, 'old': old})
            return parent, name
        except Exception:
            os.close(parent)
            raise

    def data_parent(self, staging_parent):
        return os.dup(staging_parent), self.data_name().name

    def committed(self, manager, staging_fd):
        # Later operations pin the data directory by inode; follow the swap.
        info = os.fstat(staging_fd)
        saved = self.existing.saved()
        storage.atomic(self.existing.metadata, {**saved, 'data_identity': node(info), 'data_owner_uid': info.st_uid})

    def settled(self, manager):
        manager.journal(None)


class Backups:
    def __init__(self, root, runtimes=(), allow_temporary=False):
        self.root = Path(root)
        self.allow_temporary = allow_temporary
        self.path = self.root / 'backups.json'
        self.internal = self.root / 'backups'
        self.lock = threading.RLock()
        self.entries, self.folders, self.pending = {}, {}, None
        self.jobs, self.controls = {}, {}
        self.digests = {}
        self.error = None
        try:
            self.load()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RuntimeError) as error:
            # Keep the file and the archives; only this feature is unavailable.
            self.error = 'The backup list cannot be read, so backups are unavailable: ' + str(error)
        if not self.error:
            self.prune(set(runtimes))

    # --- registry ---
    @staticmethod
    def valid_entry(key, entry):
        return (isinstance(key, str) and re.fullmatch(BACKUP, key) is not None and isinstance(entry, dict)
                and isinstance(entry.get('runtime_id'), str)
                and (entry['runtime_id'] == 'default' or re.fullmatch(RUNTIME, entry['runtime_id']) is not None)
                and type(entry.get('owner_uid')) is int and entry['owner_uid'] >= 0
                and all(type(entry.get(field)) is int and entry[field] >= 0 for field in ('created', 'archive_bytes', 'data_bytes'))
                and isinstance(entry.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) is not None
                and (entry.get('folder') is None or storage.valid_location(entry['folder'], FOLDER_NAME))
                and (entry.get('image') is None or (isinstance(entry['image'], dict) and set(entry['image']) == {'system', 'vendor'}
                     and all(isinstance(value, str) for value in entry['image'].values())))
                and (entry.get('sdk') is None or type(entry['sdk']) is int)
                and isinstance(entry.get('image_label', ''), str) and isinstance(entry.get('device_name', ''), str))

    def load(self):
        data = read_state(self.path)
        if (not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('backups'), dict)
                or not isinstance(data.get('folders'), dict) or len(data['backups']) > MAX_TOTAL
                or not all(self.valid_entry(key, entry) for key, entry in data['backups'].items())
                or not all(re.fullmatch(r'[0-9]{1,10}', key) and storage.valid_location(value, FOLDER_NAME)
                           for key, value in data['folders'].items())):
            raise RuntimeError('unsupported or invalid backups.json')
        pending = data.get('restore')
        if pending is not None and not (
                isinstance(pending, dict) and isinstance(pending.get('parent'), str) and Path(pending['parent']).is_absolute()
                and isinstance(pending.get('staging'), str) and re.fullmatch(re.escape(DEFAULT_STAGING) + r'[0-9a-f]{8}', pending['staging'])
                and all(isinstance(pending.get(key), dict) and set(pending[key]) == {'device', 'inode'}
                        and all(type(value) is int for value in pending[key].values()) for key in ('new', 'old'))):
            raise RuntimeError('invalid restore journal in backups.json')
        self.entries, self.folders, self.pending = data['backups'], data['folders'], pending

    def save(self):
        with self.lock:
            data = {'version': 1, 'backups': copy.deepcopy(self.entries), 'folders': copy.deepcopy(self.folders)}
            if self.pending: data['restore'] = copy.deepcopy(self.pending)
            storage.atomic(self.path, data)

    def journal(self, value):
        with self.lock:
            self.pending = value
            self.save()

    def prune(self, runtimes):
        """Archives of devices that no longer exist can never be restored."""
        for identifier in {entry['runtime_id'] for entry in self.entries.values()} - runtimes - {'default'}:
            self.forget(identifier)

    def forget(self, identifier):
        """Remove every backup of a deleted device; returns how many files were out of reach."""
        with self.lock:
            keys = [key for key, entry in self.entries.items() if entry['runtime_id'] == identifier]
            self.jobs.pop(identifier, None)
            self.controls.pop(identifier, None)
        return sum(not self.remove(key) for key in keys)

    def backing_up(self):
        return sum(1 for job in self.jobs.values() if job['status'] == 'Running' and job['action'] == 'backup')

    def full(self, identifier, uid, extra=0):
        """Why no further backup may be recorded, or None. Enforced when a backup is
        requested and again when it is recorded, so the registry always loads."""
        with self.lock:
            device = sum(1 for entry in self.entries.values() if entry['runtime_id'] == identifier and entry['owner_uid'] == uid)
            user = sum(1 for entry in self.entries.values() if entry['owner_uid'] == uid)
            if device + extra >= MAX_PER_DEVICE:
                return f'This device already has {MAX_PER_DEVICE} backups. Delete one before making another.'
            if user + extra >= MAX_PER_USER:
                return f'Your devices already have {MAX_PER_USER} backups in total. Delete some before making another.'
            if len(self.entries) + extra >= MAX_TOTAL:
                return 'This computer has reached the limit for stored backups. Delete some before making another.'
        return None

    # --- storage folders ---
    def open_folder(self, folder):
        """Pinned descriptor of a private directory that holds archives."""
        if folder is None:
            self.internal.mkdir(mode=0o700, exist_ok=True)
            fd = storage.open_directory(self.internal)
            try: private_identity(fd)
            except Exception:
                os.close(fd)
                raise
            return fd
        try:
            fd = storage.open_directory(folder['path'])
        except OSError as error:
            raise RuntimeError('The backup folder is unavailable. Connect the disk that holds your data folder.') from error
        try:
            if not storage.same_identity(private_identity(fd), folder):
                raise RuntimeError('The backup folder was replaced or its disk is not mounted')
        except Exception:
            os.close(fd)
            raise
        return fd

    def destination(self, uid, location):
        """Folder for a new archive: the caller's data folder, else the controller store."""
        if not location:
            return None, self.open_folder(None)
        with self.lock:
            parent = storage.external_parent(location['path'], uid, self.root, self.allow_temporary)
            try:
                if not storage.same_identity(storage.folder_identity(parent), location):
                    raise RuntimeError('The data folder was replaced or its disk is not mounted. Choose it again in Settings > Storage.')
                known = self.folders.get(str(uid))
                if known and Path(known['path']).parent == Path(location['path']):
                    try: return known, self.open_folder(known)
                    except (OSError, RuntimeError): pass  # removed: start a new folder below
                name, fd = storage.private_child(parent, PREFIX, self.allow_temporary)
            finally:
                os.close(parent)
            try:
                folder = dict(path=str(Path(location['path']) / name), **private_identity(fd))
                self.folders[str(uid)] = folder
                self.save()
            except Exception:
                os.close(fd)
                raise
            return folder, fd

    def sweep(self, folder, fd):
        """Drop unfinished archives and archives the registry no longer knows."""
        pattern = BACKUP + '(?:' + re.escape(SUFFIX) + '|' + re.escape(PARTIAL) + ')'
        # List first: every file seen here already belonged to an entry or a registered job
        # when the keep-set is read, so nothing newer can be mistaken for a stray.
        names = [name for name in os.listdir(fd) if re.fullmatch(pattern, name)]
        with self.lock:
            keep = {key + SUFFIX for key, entry in self.entries.items() if entry.get('folder') == folder}
            for job in self.jobs.values():
                if job['status'] == 'Running' and job['action'] == 'backup':
                    keep |= {job['backup_id'] + PARTIAL, job['backup_id'] + SUFFIX}
        for name in names:  # unlinking gigabytes must not hold the lock reports need
            if name in keep: continue
            try:
                if stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode): os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass

    def remove(self, key):
        """Delete one archive and forget it. Returns False when its file was out of reach:
        the entry goes anyway, so a disconnected disk cannot pin entries in the registry."""
        with self.lock:
            entry = self.entries.get(key)
            if entry is None: return True
            folder = entry.get('folder')
        reached, empty = True, False
        try:
            fd = self.open_folder(folder)  # slow part outside the lock
            try:
                try: os.unlink(key + SUFFIX, dir_fd=fd)
                except FileNotFoundError: pass
                os.fsync(fd)
                empty = folder is not None and not os.listdir(fd)
            finally:
                os.close(fd)
        except (OSError, RuntimeError):
            reached = False  # swept as a stray if the folder is ever used again
        with self.lock:
            self.entries.pop(key, None)
            # A backup that already chose this folder may be about to create its file in it.
            if empty and not self.backing_up():
                try:
                    parent = storage.open_directory(Path(folder['path']).parent)
                    try: os.rmdir(Path(folder['path']).name, dir_fd=parent)
                    finally: os.close(parent)
                    # No empty hidden folder is left in the user's data folder.
                    for uid in [uid for uid, known in self.folders.items() if known == folder]:
                        del self.folders[uid]
                except (OSError, RuntimeError):
                    pass
            self.save()
        return reached

    def claim(self, control, fd, needed):
        """Free space on fd's disk after what other running jobs still expect to write.
        Registers this job's own need when it fits."""
        with self.lock:
            device = os.fstat(fd).st_dev
            others = sum(max(0, other.budget - other.written) for other in self.controls.values()
                         if other is not control and other.device == device)
            available = free_bytes(fd) - others
            if available >= needed:
                control.device, control.budget, control.written = device, needed, 0
            return max(0, available)

    # --- Android image identity ---
    def default_image(self, existing, compute):
        """SHA-256 of System Waydroid's images; hashing is cached per file version."""
        try:
            directory = Path(existing.config().get('waydroid', 'images_path', fallback=''))
            if not directory.is_absolute(): return None
            result = {}
            for kind in ('system', 'vendor'):
                fd = os.open(directory / (kind + '.img'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode): return None
                    key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    value = self.digests.get(key)
                    if value is None:
                        if not compute: return None
                        digest = hashlib.sha256()
                        while True:
                            block = os.read(fd, CHUNK)
                            if not block: break
                            digest.update(block)
                        value = digest.hexdigest()
                        with self.lock:
                            if len(self.digests) >= 8: self.digests.clear()
                            self.digests[key] = value
                    result[kind] = value
                finally:
                    os.close(fd)
            return result
        except (OSError, RuntimeError):
            return None

    # --- interrupted System Waydroid restore ---
    def recover_default(self, existing):
        """Finish or undo a System Waydroid restore the controller did not complete.
        The journal stays while the previous data may still be mounted."""
        journal = self.pending  # only one System Waydroid job runs at a time
        if not journal: return
        saved = existing.saved()
        path = Path(saved.get('data_path', '')) if saved else Path('')
        if saved and str(path.parent) == journal['parent']:
            parent = storage.open_directory(path.parent)
            try:
                try:
                    fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try: info = os.fstat(fd)
                    finally: os.close(fd)
                except FileNotFoundError:
                    info = None
                current = node(info) if info is not None else None
                if current == journal['new']:
                    # The swap happened: the restored data is live. Record what committed() records.
                    if saved.get('data_identity') != current or saved.get('data_owner_uid') != info.st_uid:
                        storage.atomic(existing.metadata, {**saved, 'data_identity': current, 'data_owner_uid': info.st_uid})
                    if waydroid_state(existing) != 'STOPPED':
                        return  # a Waydroid started mid-restore may still use the previous copy
                    remove_named(parent, journal['staging'], journal['old'])
                elif current == journal['old']:
                    remove_named(parent, journal['staging'], journal['new'])  # never live
            finally:
                os.close(parent)
        self.journal(None)

    def clean_managed(self, store):
        """After a controller start: remove what an interrupted restore left in stopped devices."""
        with store.mutex:
            records = [dict(record) for record in store.data['runtimes']]
        for record in records:
            try:
                target = ManagedTarget(store, record)
                if not os.path.lexists(target.instance / MANAGED_STAGING): continue
                with store.mutex:
                    if target.id in store.active_jobs or target.id in store.deletions or store.storage.busy is not None: continue
                    store.active_jobs.add(target.id)
                try:
                    with target.hold():  # fails while Android runs: retried by the next backup or restore
                        target.recover(self)
                finally:
                    with store.mutex: store.active_jobs.discard(target.id)
            except (OSError, RuntimeError):
                continue

    # --- jobs ---
    def running(self, identifier):
        job = self.jobs.get(identifier)
        return bool(job and job['status'] == 'Running')

    def cancel(self, identifier):
        control = self.controls.get(identifier)
        if not self.running(identifier) or control is None or not control.cancel():
            raise RuntimeError('Nothing can be cancelled right now')

    def cancel_all(self):
        for control in list(self.controls.values()):
            control.cancel()

    def start(self, store, target, action, backup_id, runner, *args):
        job = {'id': uuid.uuid4().hex, 'action': action, 'backup_id': backup_id, 'status': 'Running',
               'phase': 'Starting…', 'done_bytes': 0, 'total_bytes': None, 'cancellable': True, 'error': None,
               'warning': None}
        control = Control()
        with self.lock:
            self.jobs[target.id] = job
            self.controls[target.id] = control
        store.active_jobs.add(target.id)
        try:
            store.pool.submit(runner, store, target, job, control, *args)
        except Exception:
            store.active_jobs.discard(target.id)
            job.update(status='Failed', error='The controller could not start the operation', cancellable=False)
            raise

    def finish(self, store, target, job, error=None, cancelled=False, control=None):
        if control is not None:
            control.device, control.budget = None, 0
        job.update(status='Cancelled' if cancelled else 'Failed' if error else 'Succeeded', cancellable=False,
                   error=None if cancelled else (str(error)[-1024:] or 'Operation failed') if error else None)
        with store.mutex:
            store.active_jobs.discard(target.id)
        changed = getattr(store, 'changed', None)
        if changed is not None:
            with changed: changed.notify_all()

    @staticmethod
    def step(job, phase, total=None):
        job.update(phase=phase, done_bytes=0, total_bytes=total)

    def run_backup(self, store, target, job, control, location):
        folder_fd = source = output = None
        partial = job['backup_id'] + PARTIAL
        written = False
        error = None
        cancelled = False
        try:
            with target.hold():
                target.recover(self)
                self.step(job, 'Measuring Android data…')
                source = target.source()
                total, entries = measure(source, control.cancelled)
                self.step(job, 'Checking the Android image…')
                image, sdk, label = target.image(self)
                folder, folder_fd = self.destination(target.uid, location)
                # What tar may emit for the measured tree; gzip grows data by a fraction at most.
                stream = total + entries * ENTRY_OVERHEAD + MARGIN
                reserve = INTERNAL_RESERVE if folder is None else 0
                needed = stream + total // 512 + reserve
                available = self.claim(control, folder_fd, needed)
                if available < needed:
                    raise RuntimeError(
                        f'Not enough free space for this backup: it needs up to {gib(needed)} and {gib(available)} is free in '
                        + ('the controller folder. Choose a data folder on a larger disk in Settings > Storage, then back up again.'
                           if folder is None else 'your data folder. Free some space or choose another data folder in Settings > Storage.'))
                self.sweep(folder, folder_fd)
                output = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder_fd)
                written = True
                self.step(job, 'Backing up Android data…', total)
                target.check(final=True)
                sha256, archive_bytes, data_bytes = create_archive(source, output, job, control, target.check,
                                                                   limit=stream, floor=reserve + MARGIN // 4)
                target.check(final=True)
                os.fsync(output)
                control.commit()
                job['cancellable'] = False
                with self.lock:
                    full = self.full(target.id, target.uid)
                    if full: raise RuntimeError(full)
                    os.rename(partial, job['backup_id'] + SUFFIX, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
                    written = False
                    os.fsync(folder_fd)
                    self.entries[job['backup_id']] = {
                        'runtime_id': target.id, 'owner_uid': target.uid, 'created': int(time.time()),
                        'archive_bytes': archive_bytes, 'data_bytes': data_bytes, 'sha256': sha256, 'folder': folder,
                        'image': image, 'sdk': sdk, 'image_label': label, 'device_name': target.name[:128]}
                    self.save()
        except Cancelled:
            cancelled = True
        except OSError as failure:
            error = ('The disk filled up during the backup, so it was abandoned. Free some space or choose another data folder in Settings > Storage.'
                     if failure.errno == errno.ENOSPC else f'Backup failed: {reason(failure)}.')
        except Exception as failure:
            error = reason(failure)
        finally:
            for fd in (output, source):
                if fd is not None: os.close(fd)
            if folder_fd is not None:
                if written:
                    try: os.unlink(partial, dir_fd=folder_fd)
                    except OSError: pass
                os.close(folder_fd)
            self.finish(store, target, job, error, cancelled, control)

    def run_restore(self, store, target, job, control, entry):
        folder_fd = archive = old = staging_parent = staging_fd = data_parent = None
        staging_name = None
        swapped = False
        error = None
        cancelled = False
        try:
            with target.hold():
                target.recover(self)
                folder_fd = self.open_folder(entry.get('folder'))
                try: archive = os.open(job['backup_id'] + SUFFIX, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder_fd)
                except FileNotFoundError: raise RuntimeError('The backup file is missing. Nothing was changed.') from None
                self.step(job, 'Verifying the backup…', entry['archive_bytes'])
                verify_archive(archive, entry, job, control)
                old = target.source(required=False)
                staging_parent, staging_name = target.staging(self, control)
                staging_fd = os.open(staging_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=staging_parent)
                private_identity(staging_fd)
                if os.listdir(staging_fd): raise RuntimeError('Restore staging directory was changed')
                staged = node(os.fstat(staging_fd))
                needed = entry['data_bytes'] + MARGIN
                available = self.claim(control, staging_fd, needed)
                if available < needed:
                    raise RuntimeError(
                        f'Not enough free space to restore: {gib(needed)} is needed next to the current Android data and '
                        f'{gib(available)} is free. The current data is kept until the restore succeeds. Free some space and try again.')
                self.step(job, 'Restoring Android data…', entry['data_bytes'])
                target.check(final=True)
                extract_archive(archive, staging_fd, entry, job, control, target.check)
                self.step(job, 'Checking the restored data…')
                measure(staging_fd, control.cancelled)
                os.sync()  # the restored copy must be on disk before the old one goes
                data_parent, data_name = target.data_parent(staging_parent)
                target.check(final=True)
                control.commit()  # from here the restore runs to completion
                job['cancellable'] = False
                self.step(job, 'Switching to the restored data…')
                if old is None:
                    os.rename(staging_name, data_name, src_dir_fd=staging_parent, dst_dir_fd=data_parent)
                else:
                    if node(os.stat(data_name, dir_fd=data_parent, follow_symlinks=False)) != node(os.fstat(old)):
                        raise RuntimeError('The Android data folder changed during the restore. Nothing was changed.')
                    try:
                        exchange(staging_parent, staging_name, data_parent, data_name)
                    except OSError as failure:
                        raise RuntimeError('This disk cannot switch folders in one step, so the restore was not applied. '
                                           'Nothing was changed. (' + reason(failure) + ')') from failure
                swapped = True
                # The device now runs on the restored data. Whatever fails below is
                # clean-up: report it as such, never as a failed restore.
                try:
                    if node(os.stat(data_name, dir_fd=data_parent, follow_symlinks=False)) != staged:
                        raise RuntimeError('the Android data folder was changed by something else at the same moment; '
                                           'check this device before relying on it')
                    os.fsync(data_parent)
                    os.fsync(staging_parent)
                    target.committed(self, staging_fd)
                    if old is None:
                        target.settled(self)
                    elif target.removable():
                        self.step(job, 'Removing the previous data…')
                        remove_named(staging_parent, staging_name, node(os.fstat(old)))
                        target.settled(self)
                    else:
                        # Its recovery journal stays, so the copy is removed once Waydroid is stopped.
                        job['warning'] = ('The device was restored, but System Waydroid was started before the previous data '
                                          'could be removed. Stop System Waydroid completely; the previous data is then removed '
                                          'before the next backup or restore.')
                except Exception as failure:
                    job['warning'] = ('The device was restored from the backup, but cleaning up afterwards failed: '
                                      + reason(failure) + '. The previous Android data may still take up disk space; '
                                      'it is removed automatically before the next backup or restore of this device.')
        except Cancelled:
            cancelled = True
        except OSError as failure:
            error = f'Restore failed: {reason(failure)}. Nothing was changed.'
        except Exception as failure:
            error = reason(failure)
        finally:
            if not swapped and staging_fd is not None:
                try:
                    remove_named(staging_parent, staging_name, node(os.fstat(staging_fd)))
                    target.settled(self)
                except (OSError, RuntimeError):
                    pass  # recovered before the next backup or restore
            for fd in (archive, folder_fd, old, staging_fd, staging_parent, data_parent):
                if fd is not None: os.close(fd)
            self.finish(store, target, job, error, cancelled, control)

    # --- reports ---
    def owned(self, key, target, uid):
        entry = self.entries.get(key) if isinstance(key, str) and re.fullmatch(BACKUP, key) else None
        if entry is None or entry['runtime_id'] != target.id or entry['owner_uid'] != uid:
            raise RuntimeError('Backup not found for this device')
        return entry

    def report(self, store, target, uid):
        try:
            current = target.image(self, compute=False)[0]
        except (OSError, RuntimeError, ValueError):
            current = None
        reachable = {}

        def available(key, entry):
            folder = entry.get('folder')
            token = json.dumps(folder, sort_keys=True)
            if token not in reachable:
                try: reachable[token] = self.open_folder(folder)
                except (OSError, RuntimeError): reachable[token] = None
            fd = reachable[token]
            if fd is None: return False
            try: return stat.S_ISREG(os.stat(key + SUFFIX, dir_fd=fd, follow_symlinks=False).st_mode)
            except OSError: return False

        with self.lock:
            try:
                rows = [{'id': key, 'created': entry['created'], 'archive_bytes': entry['archive_bytes'],
                         'data_bytes': entry['data_bytes'], 'sdk': entry.get('sdk'), 'image_label': entry.get('image_label', ''),
                         'device_name': entry.get('device_name', ''),
                         # Unknown (null) until System Waydroid's images have been hashed once.
                         'image_matches': None if current is None or entry.get('image') is None else entry['image'] == current,
                         'available': available(key, entry)}
                        for key, entry in self.entries.items() if entry['runtime_id'] == target.id and entry['owner_uid'] == uid]
            finally:
                for fd in reachable.values():
                    if fd is not None: os.close(fd)
            job = copy.deepcopy(self.jobs.get(target.id))
        rows.sort(key=lambda row: (row['created'], row['id']), reverse=True)
        place = store.data_locations.report(uid)
        location = {'path': place['path'] or str(self.internal), 'external': bool(place['path']),
                    'available': place['available'], 'error': place['error'], 'free_bytes': place['free_bytes']}
        try:
            blocked = target.blocked()
        except (OSError, RuntimeError) as error:
            blocked = str(error)
        return {'runtime_id': target.id, 'backups': rows, 'job': job, 'location': location, 'notice': None,
                'ready': blocked is None, 'blocked': blocked, 'limit': self.full(target.id, uid), 'has_data': target.has_data()}


def authorize(store, identifier, uid):
    """Owner check only: status and cancel must work while a job holds the device."""
    if identifier == 'default':
        _, saved = store.existing.authorize(uid)
        if not saved or saved.get('owner_uid') != uid:
            raise RuntimeError('Enable management for System Waydroid before using backups')
        return DefaultTarget(store, uid, saved)
    if not isinstance(identifier, str) or not re.fullmatch(RUNTIME, identifier):
        raise RuntimeError('Invalid runtime ID')
    record = next((row for row in store.data['runtimes'] if row['id'] == identifier and row['owner_uid'] == uid), None)
    if record is None:
        raise RuntimeError('Runtime not found for this user')
    if identifier in store.deletions:
        raise RuntimeError('Deletion is incomplete; backups are unavailable')
    return ManagedTarget(store, record)


def dispatch(store, request, uid):
    op = request['op']
    target = authorize(store, request['id'], uid)  # ownership before anything else
    manager = store.backups
    if manager.error:
        raise RuntimeError(manager.error)
    if op == 'backups_get':
        return manager.report(store, target, uid)
    if op == 'backup_cancel':
        manager.cancel(target.id)
        return manager.report(store, target, uid)
    entry = manager.owned(request['backup_id'], target, uid) if 'backup_id' in request else None
    if manager.running(target.id) or target.id in store.active_jobs:
        raise RuntimeError('Wait for the current device operation to finish.')
    if op == 'backup_delete':
        # Unlinking and syncing a multi-gigabyte archive: hold only this device, not the controller.
        with store.reserve_runtime(target.id, 'Wait for the current device operation to finish.'):
            reached = manager.remove(request['backup_id'])
        report = manager.report(store, target, uid)
        if not reached:
            report['notice'] = ('The backup was removed from the list, but its file could not be deleted because the backup '
                                'folder is not reachable (its disk may be disconnected). The file is deleted the next time a '
                                'backup is saved to that folder.')
        return report
    if op == 'backup_restore' and type(request['allow_image_mismatch']) is not bool:
        raise RuntimeError('allow_image_mismatch must be boolean')
    blocked = target.blocked()
    if blocked:
        raise RuntimeError(blocked)
    if op == 'backup_create':
        full = manager.full(target.id, uid, manager.backing_up())  # backups still running count too
        if full:
            raise RuntimeError(full)
        require_gnu_tar()
        manager.start(store, target, 'backup', uuid.uuid4().hex, manager.run_backup, store.data_locations.get(uid))
        return manager.report(store, target, uid)
    if op == 'backup_restore':
        require_gnu_tar()
        if not request['allow_image_mismatch']:
            # Hashing System Waydroid's images takes seconds: hold only this device.
            with store.reserve_runtime(target.id, 'Wait for the current device operation to finish.'):
                current = target.image(manager, compute=True)[0]
            if entry.get('image') is None or current != entry['image']:
                raise RuntimeError(IMAGE_MISMATCH + ' Apps and settings from it may not work with the image this device '
                                   'uses now. Restore it only if you accept that risk.')
            blocked = target.blocked()
            if blocked:
                raise RuntimeError(blocked)
            entry = manager.owned(request['backup_id'], target, uid)
        manager.start(store, target, 'restore', request['backup_id'], manager.run_restore, copy.deepcopy(entry))
        return manager.report(store, target, uid)
    raise RuntimeError('Unsupported backup operation')
