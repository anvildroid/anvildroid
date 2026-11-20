#!/usr/bin/env python3
"""System Waydroid sees the desktop user's runtime folder after a boot-time start."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('default_session_folder', Path(__file__).with_name('runtime-system.py'))
system = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system)


class SessionFolder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base = root / 'ours/run/user'
        self.host = root / 'host'
        self.calls = []

    def sync(self, uid='1000', mounted=False):
        relative = self.base.relative_to('/')
        (self.host / relative).parent.mkdir(parents=True, exist_ok=True)
        return system.sync_user_runtime(uid, base=self.base, host=self.host,
                                        run=lambda *argv: self.calls.append(list(map(str, argv))),
                                        mounted=lambda path: mounted)

    def login(self, uid='1000'):
        path = self.host / self.base.relative_to('/') / uid
        path.mkdir(parents=True)
        return path

    def test_nobody_logged_in_changes_nothing(self):
        self.assertFalse(self.sync())
        self.assertEqual(self.calls, [])

    def test_folder_that_appeared_after_boot_is_attached_and_kept_private(self):
        self.login()
        (self.base / '1000').mkdir(parents=True)          # the empty directory under the login mount
        self.assertTrue(self.sync())
        local = str(self.base / '1000')
        self.assertEqual(self.calls, [
            [sys.executable, '-I', '-S', '-c', system.ATTACH_HOST_MOUNT, local],
            ['mount', '--make-private', local]])

    def test_missing_local_directory_is_created_first(self):
        self.login()
        self.assertTrue(self.sync())
        self.assertTrue((self.base / '1000').is_dir())

    def test_same_directory_is_left_alone(self):
        host = self.login()
        self.base.mkdir(parents=True)
        os.symlink(host, self.base / '1000')              # stat() then reports the very same directory
        self.assertFalse(self.sync())
        self.assertEqual(self.calls, [])

    def test_folder_of_an_earlier_login_is_replaced(self):
        self.login()
        (self.base / '1000').mkdir(parents=True)
        self.assertTrue(self.sync(mounted=True))
        self.assertEqual(self.calls[0], ['umount', '-l', str(self.base / '1000')])
        self.assertEqual(len(self.calls), 3)

    def test_only_numeric_user_ids_reach_a_command(self):
        self.login()
        for uid in ('', '../1000', '1000/..', '-1', 'huy', '1' * 11, None):
            self.assertFalse(self.sync(uid))
        self.assertEqual(self.calls, [])

    def test_helper_text_is_constant_and_takes_the_path_as_argument(self):
        self.assertNotIn('1000', system.ATTACH_HOST_MOUNT)
        self.assertIn('sys.argv[1]', system.ATTACH_HOST_MOUNT)
        compile(system.ATTACH_HOST_MOUNT, 'attach', 'exec')

    def test_start_hook_calls_it_before_the_container_is_configured(self):
        source = Path(system.__file__).read_text()
        start = source.index('    def start(self, args, session):')
        self.assertLess(source.index('sync_user_runtime(session.get', start), source.index('self.original_start(args, session)', start + 400))


if __name__ == '__main__':
    unittest.main()
