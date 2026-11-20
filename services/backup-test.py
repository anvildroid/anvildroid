"""Device backups: real tar archives and swaps in temporary directories, fake store.
Runs as any user; the device-file case needs root (sudo python3 -B services/backup-test.py)."""
import contextlib
import errno
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


features = module('runtime-features')
existing_module = module('runtime-existing')
backup = features.backup
storage = backup.storage
UID = os.getuid()
FIRST, SECOND = 'r-' + '1' * 32, 'r-' + '2' * 32
SYSTEM, VENDOR = 'a' * 64, 'b' * 64


class Inline:
    def submit(self, function, *args):
        function(*args)


class Threads:
    def __init__(self): self.threads = []
    def submit(self, function, *args):
        thread = threading.Thread(target=function, args=args)
        self.threads.append(thread)
        thread.start()
    def join(self):
        for thread in self.threads: thread.join(30)


class Locations:
    def __init__(self, root): self.root, self.entry = root, None
    def get(self, uid): return dict(self.entry) if self.entry and uid == UID else None
    def report(self, uid):
        return {'path': self.entry['path'] if self.entry and uid == UID else None, 'available': True, 'error': None, 'free_bytes': 10 * 1024**3}


class Store:
    def __init__(self, root, pool=None):
        self.root = root
        self.mutex = threading.RLock()
        self.changed = threading.Condition(threading.Lock())
        self.active_jobs, self.deletions, self.imports, self.app_jobs, self.selections = set(), {}, {}, {}, {}
        self.data = {'runtimes': [{'id': FIRST, 'owner_uid': UID, 'name': 'Phone', 'state': 'Stopped'},
                                  {'id': SECOND, 'owner_uid': UID + 1, 'name': 'Other', 'state': 'Stopped'}]}
        self.quiet = True
        self.reserved = []
        self.storage = types.SimpleNamespace(busy=None, ensure=lambda identifier: None)
        self.backend = types.SimpleNamespace(location=lambda identifier: root / 'instances' / identifier)
        self.android = types.SimpleNamespace(quiescent=lambda identifier: self.quiet)
        self.data_locations = Locations(root)
        self.pool = pool or Inline()
        self.existing = None
        self.backups = backup.Backups(root, {FIRST, SECOND}, allow_temporary=True)

    @contextlib.contextmanager
    def reserve_runtime(self, identifier, message='busy'):
        if identifier in self.active_jobs: raise RuntimeError(message)
        self.reserved.append(identifier)
        self.active_jobs.add(identifier)
        try: yield
        finally: self.active_jobs.discard(identifier)


def xattrs_supported(path):
    try:
        os.setxattr(path, 'user.anvil_probe', b'1')
        os.removexattr(path, 'user.anvil_probe')
        return True
    except OSError:
        return False


def fill(data):
    """A small tree with what Android data relies on: modes, links, FIFOs and xattrs."""
    (data / 'data/com.example/files').mkdir(parents=True)
    (data / 'data/com.example/cache').mkdir()
    (data / 'empty').mkdir(mode=0o700)
    os.chmod(data, 0o771)
    os.chmod(data / 'data/com.example', 0o700)
    (data / 'data/com.example/files/save.bin').write_bytes(b'progress' * 4096)
    os.chmod(data / 'data/com.example/files/save.bin', 0o640)
    (data / 'app.apk').write_bytes(os.urandom(70000))
    os.link(data / 'app.apk', data / 'data/com.example/base.apk')
    os.symlink('/data/app/lib', data / 'data/com.example/lib')
    os.symlink('../../outside', data / 'escape')
    os.mkfifo(data / 'data/com.example/pipe', 0o600)
    if xattrs_supported(data):
        os.setxattr(data / 'data/com.example/cache', 'user.inode_cache', b'\x01\x00\x02')
        os.setxattr(data / 'app.apk', 'user.note', b'apk')


def fake_archive(members, headers=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            if headers: info.pax_headers = dict(headers)
            info.uid, info.gid = os.getuid(), os.getgid()
            if kind == 'symlink':
                info.type, info.linkname = tarfile.SYMTYPE, target
                archive.addfile(info)
            elif kind == 'link':
                info.type, info.linkname = tarfile.LNKTYPE, target
                archive.addfile(info)
            else:
                info.size = 4
                archive.addfile(info, io.BytesIO(b'evil'))
    return buffer.getvalue()


class Base(unittest.TestCase):
    pool = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anvil-backup-', dir=os.environ.get('ANVIL_TEST_TMP', '/tmp'))
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'store'
        self.root.mkdir(mode=0o700)
        self.instance = self.root / 'instances' / FIRST
        self.data = self.instance / 'android' / FIRST / 'data'
        self.data.mkdir(parents=True)
        os.chmod(self.instance, 0o700)
        fill(self.data)
        (self.instance / 'prepared').mkdir()
        (self.instance / 'support').mkdir()
        self.write_image(SYSTEM)
        (self.instance / 'support/compatibility.json').write_text(json.dumps({'sdk': 33}))
        self.store = Store(self.root, self.pool)
        self.manager = self.store.backups

    def write_image(self, system):
        (self.instance / 'prepared/manifest.json').write_text(json.dumps(
            {'version': 1, 'runtime_id': FIRST, 'images': {'system.img': system, 'vendor.img': VENDOR}}))

    def call(self, op, uid=UID, identifier=FIRST, **fields):
        return features.dispatch(self.store, {'op': op, 'id': identifier, **fields}, uid)

    def backup(self, identifier=FIRST):
        report = self.call('backup_create', identifier=identifier)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        return next(row for row in report['backups'] if row['id'] == report['job']['backup_id'])

    def archive(self, row, folder=None):
        return (folder or self.root / 'backups') / (row['id'] + backup.SUFFIX)

    def forge(self, row, payload):
        """Replace an archive and make the registry vouch for it (the worst case)."""
        packed = gzip.compress(payload)
        self.archive(row).write_bytes(packed)
        self.manager.entries[row['id']].update(sha256=hashlib.sha256(packed).hexdigest(), archive_bytes=len(packed), data_bytes=len(payload))

    def restore(self, row, identifier=FIRST):
        return self.call('backup_restore', identifier=identifier, backup_id=row['id'], allow_image_mismatch=False)


class ManagedTest(Base):
    def test_backup_then_restore_reproduces_the_data_exactly(self):
        before = storage.tree_manifest(self.data)
        top = self.data.stat()
        row = self.backup()
        archive = self.archive(row)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(archive.parent.stat().st_mode), 0o700)
        entry = self.manager.entries[row['id']]
        self.assertEqual(entry['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual((row['archive_bytes'], row['sdk'], row['image_matches'], row['available']),
                         (archive.stat().st_size, 33, True, True))
        self.assertEqual(entry['image'], {'system': SYSTEM, 'vendor': VENDOR})
        self.assertGreater(row['data_bytes'], 70000)
        self.assertIs(self.call('backups_get')['has_data'], True)
        self.assertEqual(storage.tree_manifest(self.data), before)  # backing up changes nothing

        (self.data / 'data/com.example/files/save.bin').write_bytes(b'lost')
        (self.data / 'app.apk').unlink()
        (self.data / 'new-app').mkdir()
        (self.data / 'new-app/file').write_text('added after the backup')
        report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        self.assertEqual(storage.tree_manifest(self.data), before)
        restored = self.data.stat()
        self.assertEqual((stat.S_IMODE(restored.st_mode), restored.st_uid), (stat.S_IMODE(top.st_mode), top.st_uid))
        self.assertNotEqual(restored.st_ino, top.st_ino)  # swapped, not patched in place
        self.assertFalse((self.instance / backup.MANAGED_STAGING).exists())
        self.assertEqual(self.store.active_jobs, set())
        self.assertFalse((self.base / 'outside').exists())

    def test_restore_onto_a_device_without_data(self):
        before = storage.tree_manifest(self.data)
        row = self.backup()
        import shutil
        shutil.rmtree(self.data)
        report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        self.assertEqual(storage.tree_manifest(self.data), before)
        shutil.rmtree(self.data)
        report = self.call('backup_create')
        self.assertIn('no Android data yet', report['job']['error'])
        self.assertIs(report['has_data'], False)

    def test_requests_are_owner_scoped_and_carry_no_paths(self):
        row = self.backup()
        with self.assertRaisesRegex(RuntimeError, 'not found for this user'): self.call('backups_get', uid=UID + 1)
        with self.assertRaisesRegex(RuntimeError, 'not found for this user'): self.call('backup_create', identifier=SECOND)
        with self.assertRaisesRegex(RuntimeError, 'Invalid runtime ID'): self.call('backups_get', identifier='../' + FIRST)
        # Another user's device cannot name this user's backup.
        with self.assertRaisesRegex(RuntimeError, 'Backup not found'):
            self.call('backup_restore', uid=UID + 1, identifier=SECOND, backup_id=row['id'], allow_image_mismatch=True)
        with self.assertRaisesRegex(RuntimeError, 'Backup not found'):
            self.call('backup_delete', uid=UID + 1, identifier=SECOND, backup_id=row['id'])
        for backup_id in ('../' + row['id'], row['id'] + '/..', 'f' * 32, 7, None):
            with self.assertRaisesRegex(RuntimeError, 'Backup not found'):
                self.call('backup_restore', backup_id=backup_id, allow_image_mismatch=True)
        for extra in ({'path': '/etc'}, {'destination': '/tmp/x'}, {'owner_uid': 0}, {'backup_id': row['id']}):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected feature fields'): self.call('backup_create', **extra)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected feature fields'): self.call('backup_restore', backup_id=row['id'])
        with self.assertRaisesRegex(RuntimeError, 'must be boolean'):
            self.call('backup_restore', backup_id=row['id'], allow_image_mismatch='yes')
        self.assertTrue(self.archive(row).exists())

    def test_device_must_be_stopped_and_idle(self):
        record = self.store.data['runtimes'][0]
        for state in ('Running', 'Frozen', 'Starting', 'Stopping', 'Provisioning', 'Allocated'):
            record['state'] = state
            self.assertFalse(self.call('backups_get')['ready'])
            with self.assertRaisesRegex(RuntimeError, 'Stop this device'): self.call('backup_create')
        record['state'] = 'Stopped'
        self.store.quiet = False
        with self.assertRaisesRegex(RuntimeError, 'still shutting down'): self.call('backup_create')
        self.store.quiet = True
        (self.instance / 'reinstall.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'Finish setting up'): self.call('backup_create')
        (self.instance / 'reinstall.json').unlink()
        self.store.active_jobs.add(FIRST)
        with self.assertRaisesRegex(RuntimeError, 'Wait for the current'): self.call('backup_create')
        self.assertIn('backups', self.call('backups_get'))  # status still answers
        self.store.active_jobs.clear()
        self.store.deletions[FIRST] = []
        with self.assertRaisesRegex(RuntimeError, 'Deletion is incomplete'): self.call('backups_get')
        self.assertFalse((self.root / 'backups').exists() and any((self.root / 'backups').iterdir()))

    def test_worker_lock_is_held_and_respected(self):
        import fcntl
        lock = os.open(self.instance / 'worker.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = self.call('backup_create')
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertIn('still running', report['job']['error'])
        self.assertEqual(report['backups'], [])

    def test_free_space_is_checked_before_writing(self):
        with patch.object(backup, 'free_bytes', return_value=1024):
            report = self.call('backup_create')
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertRegex(report['job']['error'], 'Not enough free space.*Settings > Storage')
        self.assertEqual(list((self.root / 'backups').iterdir()), [])
        row = self.backup()
        before, inode = storage.tree_manifest(self.data), self.data.stat().st_ino
        with patch.object(backup, 'free_bytes', return_value=1024):
            report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertRegex(report['job']['error'], 'Not enough free space to restore')
        self.assertEqual((storage.tree_manifest(self.data), self.data.stat().st_ino), (before, inode))
        self.assertFalse((self.instance / backup.MANAGED_STAGING).exists())

    def test_changed_archive_is_never_unpacked(self):
        row = self.backup()
        before, inode = storage.tree_manifest(self.data), self.data.stat().st_ino
        archive = self.archive(row)
        payload = bytearray(archive.read_bytes())
        payload[len(payload) // 2] ^= 0xff
        archive.write_bytes(payload)
        with patch.object(backup, 'extract_archive', side_effect=AssertionError('unpacked a changed archive')):
            report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertIn('damaged or was changed', report['job']['error'])
        self.assertEqual((storage.tree_manifest(self.data), self.data.stat().st_ino), (before, inode))
        archive.unlink()
        report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertIn('missing', report['job']['error'])
        self.assertFalse(self.call('backups_get')['backups'][0]['available'])

    def test_hostile_archives_cannot_write_outside_the_device(self):
        """Even an archive that passes the digest check stays inside staging or fails."""
        row = self.backup()
        before, inode = storage.tree_manifest(self.data), self.data.stat().st_ino
        victim = self.base / 'victim'
        victim.mkdir()
        (victim / 'keep').write_text('safe')
        cases = {
            'parent path': [('../../../../victim/pwned', 'file', None)],
            'absolute symlink': [('x', 'symlink', str(victim)), ('x/pwned', 'file', None)],
            'relative symlink': [('x', 'symlink', '../../../../victim'), ('x/pwned', 'file', None)],
            'hard link': [('h', 'link', str(victim / 'keep'))],
        }
        for name, members in cases.items():
            with self.subTest(name):
                payload = gzip.compress(fake_archive(members))
                self.archive(row).write_bytes(payload)
                entry = self.manager.entries[row['id']]
                entry.update(sha256=hashlib.sha256(payload).hexdigest(), archive_bytes=len(payload),
                             data_bytes=len(gzip.decompress(payload)))
                report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
                self.assertEqual(report['job']['status'], 'Failed', name)
                self.assertEqual(sorted(path.name for path in victim.iterdir()), ['keep'])
                self.assertEqual((victim / 'keep').stat().st_nlink, 1)
                self.assertEqual((storage.tree_manifest(self.data), self.data.stat().st_ino), (before, inode))
                self.assertFalse((self.instance / backup.MANAGED_STAGING).exists())
        # An absolute member name is made relative by tar and stays in the device.
        payload = gzip.compress(fake_archive([(str(victim / 'pwned'), 'file', None)]))
        self.archive(row).write_bytes(payload)
        self.manager.entries[row['id']].update(sha256=hashlib.sha256(payload).hexdigest(), archive_bytes=len(payload),
                                              data_bytes=len(gzip.decompress(payload)))
        self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(sorted(path.name for path in victim.iterdir()), ['keep'])

    @unittest.skipUnless(os.geteuid() == 0, 'creating device files requires root')
    def test_device_files_are_refused(self):
        os.mknod(self.data / 'null', 0o600 | stat.S_IFCHR, os.makedev(1, 3))
        report = self.call('backup_create')
        self.assertIn('device file', report['job']['error'])
        (self.data / 'null').unlink()
        row = self.backup()
        staged = self.base / 'staged'
        staged.mkdir()
        os.mknod(staged / 'null', 0o600 | stat.S_IFCHR, os.makedev(1, 3))
        fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaisesRegex(RuntimeError, 'device file'): backup.measure(fd)
        self.assertTrue(self.archive(row).exists())

    @unittest.skipUnless(os.geteuid() == 0, 'changing owners and privileged xattrs requires root')
    def test_android_owners_setuid_and_privileged_xattrs_survive(self):
        app = self.data / 'data/com.example'
        for path, uid, gid in ((app, 10123, 10123), (app / 'files/save.bin', 10123, 10123), (app / 'cache', 10123, 20123),
                               (self.data / 'app.apk', 1000, 1000), (app / 'lib', 1000, 1000), (self.data, 1000, 1000)):
            os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(app / 'files/save.bin', 0o6750)
        os.setxattr(app / 'files/save.bin', 'trusted.overlay.test', b'x')
        os.setxattr(app / 'cache', 'security.selinux', b'u:object_r:app_data_file:s0\0')
        before = storage.tree_manifest(self.data)
        row = self.backup()
        import shutil
        shutil.rmtree(app)
        os.chown(self.data / 'app.apk', 0, 0)
        report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        self.assertEqual(storage.tree_manifest(self.data), before)
        self.assertEqual((self.data.stat().st_uid, (app / 'cache').stat().st_gid), (1000, 20123))

    def test_different_android_image_needs_explicit_consent(self):
        row = self.backup()
        self.write_image('c' * 64)
        self.assertIs(self.call('backups_get')['backups'][0]['image_matches'], False)
        inode = self.data.stat().st_ino
        with self.assertRaisesRegex(RuntimeError, '^' + backup.IMAGE_MISMATCH):
            self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual((self.data.stat().st_ino, self.store.active_jobs), (inode, set()))
        report = self.call('backup_restore', backup_id=row['id'], allow_image_mismatch=True)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        self.assertNotEqual(self.data.stat().st_ino, inode)

    def test_delete_and_device_removal_drop_archives(self):
        first, second = self.backup(), self.backup()
        self.assertEqual(len(self.call('backups_get')['backups']), 2)
        report = self.call('backup_delete', backup_id=first['id'])
        self.assertEqual([row['id'] for row in report['backups']], [second['id']])
        self.assertFalse(self.archive(first).exists())
        with self.assertRaisesRegex(RuntimeError, 'Backup not found'): self.call('backup_delete', backup_id=first['id'])
        # A restarted controller keeps the list, sweeps strays, and prunes deleted devices.
        stray = self.root / 'backups' / ('e' * 32 + backup.PARTIAL)
        stray.write_bytes(b'unfinished')
        self.store.backups = self.manager = backup.Backups(self.root, {FIRST}, allow_temporary=True)
        self.assertEqual([row['id'] for row in self.call('backups_get')['backups']], [second['id']])
        third = self.backup()
        self.assertFalse(stray.exists())
        self.manager.forget(FIRST)
        self.assertEqual((self.manager.entries, list((self.root / 'backups').iterdir())), ({}, []))
        self.assertFalse(self.archive(third).exists())
        row = self.backup()
        pruned = backup.Backups(self.root, set(), allow_temporary=True)
        self.assertEqual(pruned.entries, {})
        self.assertFalse(self.archive(row).exists())

    def test_unreadable_registry_disables_backups_without_deleting_anything(self):
        row = self.backup()
        (self.root / 'backups.json').write_text('{"version": 9}')
        os.chmod(self.root / 'backups.json', 0o600)
        self.store.backups = backup.Backups(self.root, {FIRST}, allow_temporary=True)
        with self.assertRaisesRegex(RuntimeError, 'backup list cannot be read'): self.call('backups_get')
        self.assertTrue(self.archive(row).exists())
        (self.root / 'backups.json').unlink()
        os.symlink('/etc/hostname', self.root / 'backups.json')
        self.assertIsNotNone(backup.Backups(self.root, {FIRST}, allow_temporary=True).error)

    def test_backups_go_to_the_data_folder_when_one_is_set(self):
        folder = self.base / 'disk'
        folder.mkdir(mode=0o755)
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try: storage.check_filesystem(fd, True)
            except RuntimeError: self.skipTest('temporary directory is on an unsupported filesystem')
            self.store.data_locations.entry = dict(path=str(folder), **storage.folder_identity(fd))
        finally:
            os.close(fd)
        first = self.backup()
        private = [path for path in folder.iterdir()]
        self.assertEqual(len(private), 1)
        self.assertRegex(private[0].name, '^' + backup.FOLDER_NAME + '$')
        self.assertEqual(stat.S_IMODE(private[0].stat().st_mode), 0o700)
        self.assertTrue(self.archive(first, private[0]).exists())
        self.assertFalse((self.root / 'backups').exists())
        report = self.call('backups_get')
        self.assertEqual((report['location']['path'], report['location']['external']), (str(folder), True))
        second = self.backup()
        self.assertEqual([path for path in folder.iterdir()], private)  # one folder, reused
        # A folder swapped for one the user controls is refused, not trusted.
        private[0].rename(folder / 'moved')
        (folder / private[0].name).mkdir(mode=0o777)
        os.chmod(folder / private[0].name, 0o777)
        self.assertFalse(self.call('backups_get')['backups'][0]['available'])
        report = self.call('backup_restore', backup_id=second['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Failed')
        (folder / private[0].name).rmdir()
        (folder / 'moved').rename(private[0])
        report = self.call('backup_restore', backup_id=second['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        # A data folder that disappeared never falls back to the controller store.
        valid = self.store.data_locations.entry
        self.store.data_locations.entry = dict(valid, inode=1)
        report = self.call('backup_create')
        self.assertIn('data folder was replaced', report['job']['error'])
        self.assertFalse((self.root / 'backups').exists())
        self.store.data_locations.entry = valid
        self.call('backup_delete', backup_id=first['id'])
        # Another backup may already have chosen this folder: it is not removed under it.
        self.manager.jobs[SECOND] = {'status': 'Running', 'action': 'backup', 'backup_id': 'f' * 32}
        self.call('backup_delete', backup_id=second['id'])
        self.assertEqual([path for path in folder.iterdir()], private)
        del self.manager.jobs[SECOND]
        third = self.backup()
        self.assertEqual([path for path in folder.iterdir()], private)
        self.call('backup_delete', backup_id=third['id'])
        self.assertEqual(list(folder.iterdir()), [])  # no empty hidden folder left behind


class RobustnessTest(Base):
    def test_unreachable_folder_never_pins_entries(self):
        first, second, third = self.backup(), self.backup(), self.backup()
        with patch.object(self.manager, 'open_folder', side_effect=RuntimeError('disk is gone')):
            report = self.call('backup_delete', backup_id=first['id'])
            self.assertEqual(sorted(row['id'] for row in report['backups']), sorted([second['id'], third['id']]))
            self.assertRegex(report['notice'], 'removed from the list.*could not be deleted')
            self.assertEqual(self.manager.forget(FIRST), 2)  # device deletion: both files out of reach
        self.assertEqual(self.manager.entries, {})
        self.assertEqual(json.loads((self.root / 'backups.json').read_text())['backups'], {})
        self.assertIsNone(self.call('backups_get')['notice'])
        # The files left behind are swept the next time the folder is used.
        fourth = self.backup()
        self.assertEqual(sorted(path.name for path in (self.root / 'backups').iterdir()), [fourth['id'] + backup.SUFFIX])
        # Startup pruning of a deleted device behaves the same way.
        with patch.object(backup.Backups, 'open_folder', side_effect=RuntimeError('disk is gone')):
            self.assertEqual(backup.Backups(self.root, set(), allow_temporary=True).entries, {})
        self.assertEqual(self.store.reserved, [FIRST])  # the slow unlink ran outside the store mutex

    def test_limits_hold_when_a_backup_is_requested_and_when_it_is_recorded(self):
        self.backup()
        for name, message in (('MAX_PER_DEVICE', 'This device already has 1 backups'), ('MAX_PER_USER', 'Your devices already have 1 backups'),
                              ('MAX_TOTAL', 'reached the limit for stored backups')):
            with self.subTest(name), patch.object(backup, name, 1):
                with self.assertRaisesRegex(RuntimeError, message): self.call('backup_create')
                self.assertIn(message, self.call('backups_get')['limit'])
        # A backup that is still running counts before it is recorded.
        with patch.object(backup, 'MAX_PER_DEVICE', 2):
            self.manager.jobs[SECOND] = {'status': 'Running', 'action': 'backup', 'backup_id': 'f' * 32}
            with self.assertRaisesRegex(RuntimeError, 'already has 2 backups'): self.call('backup_create')
            del self.manager.jobs[SECOND]
        # Even if the request slipped through, nothing is recorded past the limit.
        with patch.object(self.manager, 'full', side_effect=[None, 'limit reached', None, None]):
            report = self.call('backup_create')
        self.assertEqual((report['job']['status'], report['job']['error'], len(report['backups'])), ('Failed', 'limit reached', 1))
        self.assertEqual(len(list((self.root / 'backups').iterdir())), 1)
        with patch.object(backup, 'MAX_TOTAL', 1):  # what was saved always loads again
            self.assertIsNone(backup.Backups(self.root, {FIRST}, allow_temporary=True).error)

    def test_data_that_grows_or_a_filling_disk_stops_the_archive(self):
        with patch.object(backup, 'measure', return_value=(0, 0)), patch.object(backup, 'MARGIN', 0), patch.object(backup, 'INTERNAL_RESERVE', 0):
            report = self.call('backup_create')
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertIn('grew while it was being backed up', report['job']['error'])
        calls = []

        def shrinking(fd):
            calls.append(fd)
            return 10 * 1024**3 if len(calls) == 1 else 1024
        with patch.object(backup, 'free_bytes', shrinking), patch.object(backup, 'SPACE_CHECK_BYTES', 1):
            report = self.call('backup_create')
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertIn('disk is filling up', report['job']['error'])
        self.assertGreater(len(calls), 1)
        with patch.object(backup, 'write_all', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            report = self.call('backup_create')
        self.assertRegex(report['job']['error'], '^The disk filled up during the backup')
        self.assertNotIn('Errno', report['job']['error'])
        self.assertEqual((report['backups'], list((self.root / 'backups').iterdir())), ([], []))

    def test_parallel_jobs_do_not_count_the_same_free_space_twice(self):
        first, second = backup.Control(), backup.Control()
        self.manager.controls.update(one=first, two=second)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with patch.object(backup, 'free_bytes', return_value=1000):
            self.assertEqual(self.manager.claim(first, fd, 600), 1000)
            self.assertEqual((first.budget, self.manager.claim(second, fd, 600), second.budget), (600, 400, 0))
            first.written = 500  # already on disk, so already missing from the free space
            self.assertEqual((self.manager.claim(second, fd, 600), second.budget), (900, 600))
            self.manager.finish(self.store, types.SimpleNamespace(id='x'), {}, control=second)
            self.assertEqual(self.manager.claim(first, fd, 600), 1000)

    def test_failure_after_the_swap_is_reported_as_restored(self):
        before = storage.tree_manifest(self.data)
        row = self.backup()
        (self.data / 'app.apk').write_bytes(b'changed')
        with patch.object(backup, 'remove_named', side_effect=PermissionError(errno.EACCES, 'Permission denied')):
            report = self.restore(row)
        job = report['job']
        self.assertEqual((job['status'], job['error']), ('Succeeded', None))
        self.assertRegex(job['warning'], '^The device was restored from the backup, but cleaning up afterwards failed: permission denied')
        self.assertNotIn('Errno', job['warning'])
        self.assertEqual(storage.tree_manifest(self.data), before)
        leftover = self.instance / backup.MANAGED_STAGING
        self.assertEqual((leftover / 'app.apk').read_bytes(), b'changed')  # the previous data, still there
        self.backup()  # the next backup or restore of this device removes it
        self.assertFalse(leftover.exists())

    def test_stale_staging_from_a_crash_is_removed_at_start_and_before_jobs(self):
        import fcntl
        leftover = self.instance / backup.MANAGED_STAGING
        (leftover / 'data').mkdir(parents=True)
        (leftover / 'data/big').write_bytes(b'x' * 4096)
        lock = os.open(self.instance / 'worker.lock', os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.manager.clean_managed(self.store)  # Android is running: nothing is touched
        self.assertTrue((leftover / 'data/big').exists())
        os.close(lock)
        self.store.active_jobs.add(FIRST)
        self.manager.clean_managed(self.store)  # another operation holds the device
        self.assertTrue(leftover.exists())
        self.store.active_jobs.clear()
        before = storage.tree_manifest(self.data)
        self.manager.clean_managed(self.store)
        self.assertFalse(leftover.exists())
        self.assertEqual((storage.tree_manifest(self.data), self.store.active_jobs), (before, set()))
        os.symlink('/etc', leftover)  # never followed
        self.manager.clean_managed(self.store)
        self.assertTrue(leftover.is_symlink())
        leftover.unlink()
        leftover.mkdir()
        self.restore(self.backup())
        self.assertFalse(leftover.exists())

    def test_tar_warnings_fail_the_restore_and_the_backup(self):
        row = self.backup()
        before, inode = storage.tree_manifest(self.data), self.data.stat().st_ino
        # tar exits 0 when it cannot set an extended attribute.
        self.forge(row, fake_archive([('file', 'file', None)], {'SCHILY.xattr.bogus.attribute': 'value'}))
        report = self.restore(row)
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertRegex(report['job']['error'], 'could not be reproduced exactly.*Nothing was changed.*bogus.attribute')
        self.assertEqual((storage.tree_manifest(self.data), self.data.stat().st_ino), (before, inode))
        self.assertFalse((self.instance / backup.MANAGED_STAGING).exists())
        # A file dated in the future is not a defect.
        future = io.BytesIO()
        with tarfile.open(fileobj=future, mode='w', format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo('later')
            info.size, info.mtime, info.uid, info.gid = 2, 4102444800, os.getuid(), os.getgid()
            archive.addfile(info, io.BytesIO(b'ok'))
        self.forge(row, future.getvalue())
        self.assertEqual(self.restore(row)['job']['status'], 'Succeeded')
        self.assertEqual((self.data / 'later').read_bytes(), b'ok')
        with patch.object(backup, 'complaints', return_value="tar: ./x: Cannot getxattr: Permission denied"):
            report = self.call('backup_create')
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertIn('tar could not copy everything', report['job']['error'])
        self.assertEqual(len(report['backups']), 1)

    def test_other_jobs_files_survive_a_sweep_and_tar_is_checked_once(self):
        folder = self.root / 'backups'
        self.backup()
        running, stray = 'e' * 32, 'd' * 32
        for name in (running + backup.PARTIAL, running + backup.SUFFIX, stray + backup.SUFFIX, stray + backup.PARTIAL, 'notes.txt'):
            (folder / name).write_bytes(b'x')
        self.manager.jobs[SECOND] = {'status': 'Running', 'action': 'backup', 'backup_id': running}
        with patch.object(backup.subprocess, 'run', side_effect=AssertionError('tar --version ran again')):
            row = self.backup()
        self.assertEqual(sorted(path.name for path in folder.iterdir() if not path.name.startswith(row['id'])),
                         sorted([running + backup.PARTIAL, running + backup.SUFFIX, 'notes.txt'] + [key + backup.SUFFIX for key in self.manager.entries if key != row['id']]))

    def test_writing_to_a_stuck_tar_times_out(self):
        process = subprocess.Popen(['sleep', '60'], stdin=subprocess.PIPE, start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        def close():
            with contextlib.suppress(OSError): process.stdin.close()
        self.addCleanup(close)
        os.set_blocking(process.stdin.fileno(), False)
        with patch.object(backup, 'STALL_SECONDS', 0):
            with self.assertRaisesRegex(RuntimeError, 'Restore stalled'):
                backup.feed(process, b'x' * (4 * 1024 * 1024), backup.Control(), [time.monotonic()])
        cancelled = backup.Control()
        cancelled.cancel()
        with self.assertRaises(backup.Cancelled): backup.feed(process, b'x', cancelled, [time.monotonic()])


class CancelTest(Base):
    pool = None

    def setUp(self):
        self.pool = Threads()
        super().setUp()

    def test_cancel_stops_the_job_and_leaves_nothing_behind(self):
        entered, proceed = threading.Event(), threading.Event()
        real = backup.create_archive

        def slow(*args, **options):
            entered.set()
            proceed.wait(30)
            return real(*args, **options)
        with patch.object(backup, 'create_archive', slow):
            started = self.call('backup_create')
            self.assertEqual((started['job']['status'], started['job']['cancellable']), ('Running', True))
            self.assertTrue(entered.wait(30))
            self.assertEqual(self.call('backups_get')['job']['status'], 'Running')
            with self.assertRaisesRegex(RuntimeError, 'Wait for the current'): self.call('backup_create')
            self.call('backup_cancel')
            proceed.set()
            self.pool.join()
        report = self.call('backups_get')
        self.assertEqual((report['job']['status'], report['job']['error'], report['backups']), ('Cancelled', None, []))
        self.assertEqual(list((self.root / 'backups').iterdir()), [])
        self.assertEqual(self.store.active_jobs, set())
        with self.assertRaisesRegex(RuntimeError, 'Nothing can be cancelled'): self.call('backup_cancel')

    def test_cancel_during_verification_or_unpacking_leaves_the_device_alone(self):
        for stage in ('verify_archive', 'feed'):
            with self.subTest(stage):
                self.store.pool = Inline()
                row = self.backup()
                self.store.pool = pool = Threads()
                before, inode = storage.tree_manifest(self.data), self.data.stat().st_ino
                entered, proceed = threading.Event(), threading.Event()
                real = getattr(backup, stage)

                def slow(*args, **options):
                    entered.set()
                    proceed.wait(30)
                    return real(*args, **options)
                with patch.object(backup, stage, slow):
                    started = self.restore(row)
                    self.assertEqual(started['job']['status'], 'Running')
                    self.assertTrue(entered.wait(30))
                    self.call('backup_cancel')  # with a real tar waiting for input when unpacking
                    proceed.set()
                    pool.join()
                report = self.call('backups_get')
                self.assertEqual((report['job']['status'], report['job']['error'], report['job']['warning']), ('Cancelled', None, None))
                self.assertEqual((storage.tree_manifest(self.data), self.data.stat().st_ino), (before, inode))
                self.assertFalse((self.instance / backup.MANAGED_STAGING).exists())
                self.assertEqual(self.store.active_jobs, set())

    def test_cancel_kills_the_archiver_but_never_a_committed_swap(self):
        control = backup.Control()
        process = subprocess.Popen(['sleep', '60'], start_new_session=True)
        control.attach(process)
        self.assertTrue(control.cancel())
        self.assertEqual(process.wait(10), -9)
        control.detach()
        committed = backup.Control()
        committed.commit()
        self.assertFalse(committed.cancel())
        with self.assertRaises(backup.Cancelled): control.commit()


class DefaultTest(Base):
    """System Waydroid through the real ExistingRuntime adapter with a temporary work directory."""
    def setUp(self):
        super().setUp()
        self.work = self.base / 'waydroid'
        (self.work / 'images').mkdir(parents=True, mode=0o755)
        os.chmod(self.work, 0o755)
        os.chmod(self.work / 'images', 0o755)
        (self.work / 'images/system.img').write_bytes(b'system image')
        (self.work / 'images/vendor.img').write_bytes(b'vendor image')
        (self.work / 'waydroid.cfg').write_text(f'[waydroid]\nimages_path = {self.work / "images"}\n')
        os.chmod(self.work / 'waydroid.cfg', 0o644)
        self.home = self.base / 'home/waydroid'
        self.home.mkdir(parents=True)
        self.user_data = self.home / 'data'
        self.user_data.mkdir()
        fill(self.user_data)
        self.state = 'STOPPED'
        for target, value in (('session_info', lambda: {}),):
            patcher = patch.object(existing_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.existing = existing_module.ExistingRuntime(self.root, self.work)
        self.existing.state = lambda: self.state
        info = self.user_data.stat()
        storage.atomic(self.existing.metadata, {'owner_uid': UID, 'name': 'System Waydroid', 'data_path': str(self.user_data),
                                               'data_identity': {'device': info.st_dev, 'inode': info.st_ino}, 'data_owner_uid': UID})
        self.store.existing = self.existing

    def test_backup_and_restore_follow_the_data_folder(self):
        before = storage.tree_manifest(self.user_data)
        row = self.backup('default')
        self.assertEqual(self.manager.entries[row['id']]['image'],
                         {'system': hashlib.sha256(b'system image').hexdigest(), 'vendor': hashlib.sha256(b'vendor image').hexdigest()})
        self.assertEqual((row['sdk'], row['image_matches']), (None, True))
        (self.user_data / 'app.apk').write_bytes(b'broken')
        (self.user_data / 'junk').write_text('new')
        report = self.call('backup_restore', identifier='default', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        self.assertEqual(storage.tree_manifest(self.user_data), before)
        info = self.user_data.stat()
        saved = self.existing.saved()
        self.assertEqual((saved['data_identity'], saved['data_owner_uid']), ({'device': info.st_dev, 'inode': info.st_ino}, UID))
        self.assertEqual([path.name for path in self.home.iterdir()], ['data'])  # no staging or old copy left
        self.assertIsNone(self.manager.pending)
        self.assertNotIn('restore', json.loads((self.root / 'backups.json').read_text()))
        os.close(self.existing.import_source(UID))  # other System Waydroid operations still accept the folder
        self.assertEqual(self.backup('default')['image_matches'], True)

    def test_other_users_and_a_running_container_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Start System Waydroid|belongs to another'):
            self.call('backups_get', uid=UID + 1, identifier='default')
        self.state = 'RUNNING'
        with patch.object(self.existing, 'authorize', lambda uid: ({}, self.existing.saved())):
            self.assertRegex(self.call('backups_get', identifier='default')['blocked'], 'Stop System Waydroid')
            with self.assertRaisesRegex(RuntimeError, 'Stop System Waydroid'): self.call('backup_create', identifier='default')
        self.state = 'STOPPED'
        saved = self.existing.saved()
        storage.atomic(self.existing.metadata, {key: value for key, value in saved.items() if key not in ('data_identity', 'data_path')})
        with self.assertRaisesRegex(RuntimeError, 'Enable management once'): self.call('backup_create', identifier='default')

    def test_starting_waydroid_during_a_restore_aborts_before_the_swap(self):
        row = self.backup('default')
        before, inode = storage.tree_manifest(self.user_data), self.user_data.stat().st_ino
        real = backup.extract_archive

        def started(*args):
            real(*args)
            self.state = 'RUNNING'
        with patch.object(backup, 'extract_archive', started):
            report = self.call('backup_restore', identifier='default', backup_id=row['id'], allow_image_mismatch=False)
        self.assertEqual(report['job']['status'], 'Failed')
        self.assertIn('started during the operation', report['job']['error'])
        self.assertEqual((storage.tree_manifest(self.user_data), self.user_data.stat().st_ino), (before, inode))
        self.assertEqual([path.name for path in self.home.iterdir()], ['data'])
        self.assertIsNone(self.manager.pending)

    def test_waydroid_started_right_after_the_swap_keeps_the_previous_data(self):
        before = storage.tree_manifest(self.user_data)
        row = self.backup('default')
        (self.user_data / 'junk').write_text('made after the backup')
        real = backup.exchange

        def swap_then_start(*args):
            real(*args)
            self.state = 'RUNNING'
        with patch.object(backup, 'exchange', swap_then_start):
            report = self.restore(row, 'default')
        self.assertEqual(report['job']['status'], 'Succeeded')
        self.assertRegex(report['job']['warning'], 'restored, but System Waydroid was started')
        self.assertEqual(storage.tree_manifest(self.user_data), before)
        kept = [path for path in self.home.iterdir() if path.name != 'data']
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / 'junk').read_text(), 'made after the backup')  # not emptied under a running Android
        self.assertIsNotNone(self.manager.pending)
        info = self.user_data.stat()
        self.assertEqual(self.existing.saved()['data_identity'], {'device': info.st_dev, 'inode': info.st_ino})
        self.state = 'STOPPED'
        self.backup('default')  # stopped again: the kept copy goes before the next job
        self.assertEqual(([path.name for path in self.home.iterdir()], self.manager.pending), (['data'], None))

    def test_a_slow_state_answer_is_tolerated_but_never_taken_for_stopped(self):
        target = backup.DefaultTarget(self.store, UID, self.existing.saved())
        answers = []
        self.existing.state = lambda: answers.pop(0) if answers else (_ for _ in ()).throw(RuntimeError('Waydroid state command timed out'))
        with patch.object(backup.time, 'sleep'):
            answers[:] = ['STOPPED']
            target.check()
            for _ in range(backup.UNKNOWN_LIMIT - 1): target.check()  # periodic checks ride out slow answers
            with self.assertRaisesRegex(RuntimeError, 'Could not confirm'): target.check()
            fresh = backup.DefaultTarget(self.store, UID, self.existing.saved())
            with self.assertRaisesRegex(RuntimeError, 'Could not confirm'): fresh.check(final=True)  # before a swap it must be known
            self.assertIs(fresh.removable(), False)
            answers[:] = [RuntimeError, 'STOPPED']
            self.existing.state = lambda: (lambda value: value if isinstance(value, str) else (_ for _ in ()).throw(RuntimeError('timed out')))(answers.pop(0))
            fresh.check(final=True)  # one retry is enough
            answers[:] = ['RUNNING']
            with self.assertRaisesRegex(RuntimeError, 'was started during'): fresh.check()

    def test_changed_image_is_detected_by_content(self):
        row = self.backup('default')
        (self.work / 'images/system.img').write_bytes(b'another system image')
        # Unknown until hashed; the restore request hashes and refuses.
        self.assertIsNone(self.call('backups_get', identifier='default')['backups'][0]['image_matches'])
        with self.assertRaisesRegex(RuntimeError, '^' + backup.IMAGE_MISMATCH):
            self.call('backup_restore', identifier='default', backup_id=row['id'], allow_image_mismatch=False)
        self.assertIs(self.call('backups_get', identifier='default')['backups'][0]['image_matches'], False)
        self.assertEqual(self.store.active_jobs, set())

    def test_interrupted_restore_is_finished_or_undone_at_the_next_start(self):
        old = self.user_data.stat()
        staging = self.home / (backup.DEFAULT_STAGING + 'deadbeef')
        staging.mkdir(mode=0o700)
        (staging / 'restored').write_text('complete copy')
        new = staging.stat()
        journal = {'parent': str(self.home), 'staging': staging.name,
                   'new': {'device': new.st_dev, 'inode': new.st_ino}, 'old': {'device': old.st_dev, 'inode': old.st_ino}}
        # Crash before the swap: the unfinished copy is removed, the data stays.
        self.manager.journal(journal)
        restarted = backup.Backups(self.root, {FIRST}, allow_temporary=True)
        restarted.recover_default(self.existing)
        self.assertEqual(([path.name for path in self.home.iterdir()], self.user_data.stat().st_ino), (['data'], old.st_ino))
        self.assertIsNone(backup.Backups(self.root, {FIRST}, allow_temporary=True).pending)
        # Crash right after the swap: the restored copy is adopted, the old one removed.
        staging.mkdir(mode=0o700)
        (staging / 'restored').write_text('complete copy')
        new = staging.stat()
        journal.update(new={'device': new.st_dev, 'inode': new.st_ino})
        self.manager.journal(journal)
        fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        try: backup.exchange(fd, staging.name, fd, 'data')
        finally: os.close(fd)
        saved = self.existing.saved()
        storage.atomic(self.existing.metadata, {key: value for key, value in saved.items() if key != 'data_owner_uid'})
        restarted = backup.Backups(self.root, {FIRST}, allow_temporary=True)
        self.state = 'RUNNING'  # the previous copy may be mounted: adopted, but not deleted yet
        restarted.recover_default(self.existing)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), sorted(['data', staging.name]))
        self.assertIsNotNone(backup.Backups(self.root, {FIRST}, allow_temporary=True).pending)
        self.assertEqual((self.existing.saved()['data_identity'], self.existing.saved()['data_owner_uid']), (journal['new'], UID))
        self.state = 'STOPPED'
        restarted.recover_default(self.existing)
        self.assertEqual([path.name for path in self.home.iterdir()], ['data'])
        self.assertEqual((self.user_data / 'restored').read_text(), 'complete copy')
        self.assertIsNone(restarted.pending)
        os.close(self.existing.import_source(UID))


@unittest.skipUnless(os.geteuid() == 0, 'the real controller store must be root-owned')
class RealControllerTest(unittest.TestCase):
    """The real Store: its mutex, job pool, slow-operation path and device deletion."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='anvil-backup-store-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        os.chmod(base, 0o755)
        root = base / 'store'
        root.mkdir(mode=0o700)
        controller = module('runtime-controller')
        self.controller = controller
        self.root, self.images = root, base / 'images'
        self.store = controller.Store(root, self.images, allow_temporary_storage=True)
        self.addCleanup(lambda: self.store.close())
        self.uid = 4242
        record = self.store.dispatch({'op': 'create', 'name': 'Phone'}, self.uid)
        self.id = record['id']
        with self.store.mutex:
            self.store.replace(dict(record, state='Stopped', job={'id': 'a' * 32, 'operation': 'stop', 'status': 'Succeeded', 'error': None}))
        self.instance = self.store.backend.location(self.id)
        self.data = self.instance / 'android' / self.id / 'data'
        self.data.mkdir(parents=True)
        os.chmod(self.instance, 0o700)
        fill(self.data)
        os.chown(self.data / 'app.apk', 10077, 10077)

    def wait(self):
        import time
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            report = self.store.dispatch({'op': 'backups_get', 'id': self.id}, self.uid)
            if report['job'] and report['job']['status'] != 'Running': return report
            time.sleep(0.05)
        self.fail('backup job did not finish')

    def test_a_restarted_controller_removes_what_a_crashed_restore_left(self):
        leftover = self.instance / backup.MANAGED_STAGING
        (leftover / 'data').mkdir(parents=True)
        (leftover / 'data/big').write_bytes(b'x' * 4096)
        before = storage.tree_manifest(self.data)
        self.store.close()
        self.store = self.controller.Store(self.root, self.images, allow_temporary_storage=True)
        deadline = time.monotonic() + 30
        while leftover.exists() and time.monotonic() < deadline: time.sleep(0.05)
        self.assertFalse(leftover.exists())
        self.assertEqual(storage.tree_manifest(self.data), before)
        deadline = time.monotonic() + 10
        while self.store.active_jobs and time.monotonic() < deadline: time.sleep(0.05)
        self.assertTrue(self.store.idle())

    def test_backup_restore_and_device_deletion_through_the_store(self):
        before = storage.tree_manifest(self.data)
        started = self.store.dispatch({'op': 'backup_create', 'id': self.id}, self.uid)
        self.assertEqual(started['job']['action'], 'backup')
        report = self.wait()
        self.assertEqual((report['job']['status'], len(report['backups']), report['ready']), ('Succeeded', 1, True))
        self.assertEqual(report['location'], dict(report['location'], path=str(self.store.root / 'backups'), external=False))
        backup_id = report['backups'][0]['id']
        archive = self.store.root / 'backups' / (backup_id + backup.SUFFIX)
        self.assertEqual((archive.stat().st_uid, stat.S_IMODE(archive.stat().st_mode)), (0, 0o600))
        for request, uid in (({'op': 'backups_get', 'id': self.id}, self.uid + 1),
                             ({'op': 'backup_restore', 'id': self.id, 'backup_id': backup_id, 'allow_image_mismatch': True}, self.uid + 1),
                             ({'op': 'backup_delete', 'id': self.id, 'backup_id': backup_id}, self.uid + 1)):
            with self.assertRaises(self.controller.RequestError) as denied: self.store.dispatch(request, uid)
            self.assertEqual((denied.exception.code, denied.exception.message), ('FEATURE', 'Runtime not found for this user'))
        with self.assertRaises(self.controller.RequestError) as invalid:
            self.store.dispatch({'op': 'backup_create', 'id': self.id, 'path': '/root'}, self.uid)
        self.assertEqual(invalid.exception.code, 'FEATURE')
        (self.data / 'app.apk').unlink()
        # This device has no prepared image, so the backup's image is unknown: restoring needs consent.
        with self.assertRaises(self.controller.RequestError) as refused:
            self.store.dispatch({'op': 'backup_restore', 'id': self.id, 'backup_id': backup_id, 'allow_image_mismatch': False}, self.uid)
        self.assertTrue(refused.exception.message.startswith(backup.IMAGE_MISMATCH))
        self.assertEqual(self.store.active_jobs, set())
        self.store.dispatch({'op': 'backup_restore', 'id': self.id, 'backup_id': backup_id, 'allow_image_mismatch': True}, self.uid)
        report = self.wait()
        self.assertEqual(report['job']['status'], 'Succeeded', report['job'])
        self.assertEqual(storage.tree_manifest(self.data), before)
        self.assertEqual(self.store.active_jobs, set())
        self.assertTrue(self.store.idle())
        self.store.dispatch({'op': 'delete', 'id': self.id, 'confirmed_name': 'Phone'}, self.uid)
        import time
        deadline = time.monotonic() + 30
        while self.store.dispatch({'op': 'list'}, self.uid) and time.monotonic() < deadline: time.sleep(0.05)
        self.assertEqual(self.store.dispatch({'op': 'list'}, self.uid), [])
        self.assertFalse(archive.exists())
        self.assertEqual(self.store.backups.entries, {})


class ContractTest(unittest.TestCase):
    def test_field_sets_are_exact_and_registered(self):
        self.assertEqual(backup.FIELDS, {
            'backups_get': {'op', 'id'}, 'backup_create': {'op', 'id'}, 'backup_cancel': {'op', 'id'},
            'backup_restore': {'op', 'id', 'backup_id', 'allow_image_mismatch'}, 'backup_delete': {'op', 'id', 'backup_id'}})
        for op, fields in backup.FIELDS.items(): self.assertEqual(features.FIELDS[op], fields)
        controller = Path(__file__).with_name('runtime-controller.py').read_text()
        slow = controller[controller.index('SLOW_OPERATIONS = frozenset('):controller.index('IPC_WORKERS')]
        self.assertIn("'backup_restore'", slow)  # hashing System Waydroid images is a slow operation
        self.assertIn('self.backups.forget(identifier)', controller)
        self.assertIn('self.pool.submit(self.backups.clean_managed, self)', controller)
        # Device deletion unlinks archives after the store mutex is released.
        job = controller[controller.index('    def delete_job('):controller.index('    def install_job(')]
        forget = next(line for line in job.splitlines() if 'self.backups.forget(identifier)' in line)
        self.assertEqual(len(forget) - len(forget.lstrip()), 12)

    def test_tar_commands_are_fixed_and_descriptor_based(self):
        source = Path(__file__).with_name('runtime-backup.py').read_text()
        self.assertNotIn('shell=True', source)
        self.assertNotIn('os.system', source)
        self.assertEqual(source.count("f'--directory=/proc/self/fd/{"), 2)
        self.assertIn("'--numeric-owner'", source)
        self.assertIn("'--xattrs-include=*'", source)


if __name__ == '__main__':
    unittest.main()
