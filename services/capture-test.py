#!/usr/bin/env python3
"""Inert tests for device screenshots (runtime-capture.py). No Android, no root."""
import contextlib
import fcntl
import importlib.util
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib

spec = importlib.util.spec_from_file_location('runtime_capture', Path(__file__).with_name('runtime-capture.py'))
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)

ID = 'r-' + '1' * 32


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(width=1984, height=1280, pixels=4096):
    """A PNG with the header the real device produced (1984 x 1280, RGBA)."""
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return capture.SIGNATURE + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(bytes(pixels), 0)) + chunk(b'IEND', b'')


def size_of(data):
    return capture.png_size(data[:33], data[-12:], len(data))


class Store:
    """The controller's lock and reservations, reduced to what dispatch uses."""
    def __init__(self):
        self.mutex, self.reservations, self.reserved = threading.RLock(), set(), []

    @contextlib.contextmanager
    def reserve(self, key, message):
        if key in self.reservations:
            raise RuntimeError(message)
        self.reservations.add(key)
        self.reserved.append(key)
        state = self.mutex._release_save()
        try:
            yield
        finally:
            self.mutex._acquire_restore(state)
            self.reservations.discard(key)


class Png(unittest.TestCase):
    def test_real_header_is_accepted(self):
        data = png()
        # The first 24 bytes match the capture taken on the real device.
        self.assertEqual(data[:24].hex(' '), '89 50 4e 47 0d 0a 1a 0a 00 00 00 0d 49 48 44 52 00 00 07 c0 00 00 05 00')
        self.assertEqual(size_of(data), (1984, 1280))
        self.assertEqual(size_of(png(1, 1, 400)), (1, 1))
        self.assertEqual(size_of(png(16384, 16384)), (16384, 16384))

    def test_truncated_empty_and_foreign_data_are_refused(self):
        data = png()
        bad = [b'', data[:8], data[:33], data[:-1], data[:-12], data[:len(data) // 2],
               b'\xff\xd8\xff\xe0' + data[4:], b'GIF89a' + data[6:], data[:8] + b'\0\0\0\rIDAT' + data[16:],
               data[:12] + b'ihdr' + data[16:], bytes(len(data)), data + b'\0']
        for value in bad:
            with self.subTest(length=len(value)), self.assertRaisesRegex(RuntimeError, 'invalid screenshot'):
                capture.png_size(value[:33], value[-12:], len(value))

    def test_absurd_sizes_and_a_damaged_header_are_refused(self):
        for width, height in ((0, 1280), (1984, 0), (16385, 1280), (1984, 16385), (2**31, 2**31), (0xffffffff, 1)):
            with self.subTest(width=width, height=height), self.assertRaisesRegex(RuntimeError, 'invalid screenshot'):
                size_of(png(width, height))
        data = png()
        flipped = data[:20] + bytes([data[20] ^ 1]) + data[21:]     # height changed, checksum not
        with self.assertRaisesRegex(RuntimeError, 'invalid screenshot'):
            size_of(flipped)
        for size in (0, capture.MIN_PNG - 1, capture.MAX_PNG + 1, True, None, 1e6):
            with self.subTest(size=size), self.assertRaisesRegex(RuntimeError, 'invalid screenshot'):
                capture.png_size(data[:33], data[-12:], size)
        with self.assertRaisesRegex(RuntimeError, 'invalid screenshot'):
            capture.png_size(bytearray(data[:33]), data[-12:], len(data))


class Query(unittest.TestCase):
    def test_fixed_command_with_cap_and_timeout(self):
        calls = []

        def run(*args, **options):
            calls.append((args, options))
            return 4321
        self.assertEqual(capture.QUERIES['screencap'](run, {}, 9), {'bytes': 4321})
        self.assertEqual(calls, [(('screencap', '-p'), {'sink': 9, 'limit': 48 * 1024**2, 'timeout': 15})])
        self.assertNotIn('screenrecord', Path(capture.__file__).read_text())

    def test_parameters_sink_and_empty_output_are_checked(self):
        run = mock.Mock(return_value=10)
        for params, sink in (({'path': '/sdcard/x.png'}, 9), ({'display': 1}, 9), ([], 9), ({}, None), ({}, True), ({}, '9')):
            with self.subTest(params=params, sink=sink), self.assertRaises(RuntimeError):
                capture.screencap(run, params, sink)
        run.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'empty screenshot'):
            capture.screencap(mock.Mock(return_value=0), {}, 9)

    def test_declares_exactly_one_descriptor_operation(self):
        self.assertEqual(capture.FIELDS, {'screenshot_save': {'op', 'id'}})
        self.assertEqual(capture.DESCRIPTOR_OPS, {'screenshot_save'})
        self.assertEqual(set(capture.QUERIES), {'screencap'})


class Destination(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'shot.png'

    def open(self, flags=os.O_WRONLY | os.O_CREAT):
        fd = os.open(self.path, flags | os.O_CLOEXEC, 0o600)
        self.addCleanup(os.close, fd)
        return fd

    def test_access_mode_must_allow_writing(self):
        self.assertTrue(capture.writable(self.open()))
        self.assertTrue(capture.writable(self.open(os.O_RDWR)))
        self.assertFalse(capture.writable(self.open(os.O_RDONLY)))
        self.assertFalse(capture.writable(self.open(os.O_PATH)))
        folder = os.open(self.folder.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, folder)
        self.assertFalse(capture.writable(folder))
        closed = os.dup(folder)
        os.close(closed)
        self.assertFalse(capture.writable(closed))

    def test_only_regular_files_pass_the_probe(self):
        capture.probe(self.open())
        reader, writer = os.pipe()
        left, right = socket.socketpair()
        null = os.open('/dev/null', os.O_WRONLY)
        for fd in (reader, writer, null):
            self.addCleanup(os.close, fd)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        os.mkfifo(self.path.with_name('fifo'))
        fifo = os.open(self.path.with_name('fifo'), os.O_RDWR)
        self.addCleanup(os.close, fifo)
        for fd in (writer, left.fileno(), null, fifo):
            self.assertTrue(capture.writable(fd))
            with self.subTest(fd=fd), self.assertRaisesRegex(RuntimeError, 'must be a regular file'):
                capture.probe(fd)

    def test_a_destination_that_does_not_answer_is_given_up(self):
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'not responding'):
            capture.probe(self.open(), deadline=0.3, command=['/bin/sleep', '30'])
        self.assertLess(time.monotonic() - started, 5)

    def buffer(self, data):
        fd = os.memfd_create('test', os.MFD_CLOEXEC)
        self.addCleanup(os.close, fd)
        os.write(fd, data)
        return fd

    def test_copy_replaces_older_content_from_the_start(self):
        data = png()
        self.path.write_bytes(b'x' * (len(data) + 500))
        for flags in (os.O_WRONLY, os.O_RDWR | os.O_APPEND):
            fd = self.open(flags)
            os.lseek(fd, 77, os.SEEK_SET)
            capture.deliver(self.buffer(data), fd, len(data))
            self.assertEqual(self.path.read_bytes(), data)

    def test_stalled_copy_is_killed_at_the_deadline_and_leaves_an_empty_file(self):
        fd = self.open()
        # Stands in for a filesystem that accepts some bytes and then stops answering.
        stall = ['/bin/sh', '-c', 'printf "\\211PNG partial"; exec sleep 30']
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'took too long'):
            capture.deliver(self.buffer(png()), fd, 100, deadline=0.5, command=stall)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.path.read_bytes(), b'')

    def test_failed_copy_reports_the_reason_and_leaves_an_empty_file(self):
        fd = self.open()
        broken = ['/bin/sh', '-c', 'printf "\\211PNG partial"; printf "No space left on device" >&2; exit 1']
        with self.assertRaisesRegex(RuntimeError, 'Could not write the screenshot: No space left on device. Check the free space'):
            capture.deliver(self.buffer(png()), fd, 100, command=broken)
        self.assertEqual(self.path.read_bytes(), b'')
        # The real helper truncates on its own when the buffer is shorter than promised.
        self.path.write_bytes(b'old picture')
        with self.assertRaisesRegex(RuntimeError, 'ended early'):
            capture.deliver(self.buffer(b'short'), fd, 4096)
        self.assertEqual(self.path.read_bytes(), b'')

    def test_helper_text_carries_no_request_data(self):
        with mock.patch.object(capture.subprocess, 'Popen', side_effect=RuntimeError('stop')) as popen:
            with self.assertRaises(RuntimeError):
                capture.helper('copy', 5, 6, 123, 1)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:4] + argv[5:], ['-I', '-S', '-c', 'copy', '123', str(os.geteuid()), str(os.getegid())])
        self.assertIs(argv[4], capture.HELPER)
        self.assertEqual(popen.call_args.kwargs['env'], {})
        self.assertEqual((popen.call_args.kwargs['stdin'], popen.call_args.kwargs['stdout']), (5, 6))


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'shot.png'
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        self.addCleanup(os.close, self.fd)
        self.store, self.sinks = Store(), []

    def query(self, data, claim=None):
        def query(kind, params, sink=None):
            self.assertTrue(self.store.mutex._is_owned())       # the contract: called with the lock held
            self.sinks.append((kind, params, sink, os.fstat(sink)))
            os.write(sink, data)
            return {'bytes': len(data) if claim is None else claim}
        return query

    def save(self, query, descriptor=..., identifier=ID):
        with self.store.mutex:
            return capture.dispatch(self.store, {'op': 'screenshot_save', 'id': identifier}, os.getuid(), query,
                                    self.fd if descriptor is ... else descriptor)

    def test_png_reaches_the_file_and_android_only_sees_controller_memory(self):
        data = png()
        self.assertEqual(self.save(self.query(data)),
                         {'runtime_id': ID, 'bytes': len(data), 'width': 1984, 'height': 1280})
        self.assertEqual(self.path.read_bytes(), data)
        (kind, params, sink, status), = self.sinks
        self.assertEqual((kind, params), ('screencap', {}))
        target = os.fstat(self.fd)
        self.assertNotEqual(sink, self.fd)
        self.assertNotEqual((status.st_dev, status.st_ino), (target.st_dev, target.st_ino))
        with self.assertRaises(OSError):
            os.fstat(sink)                                      # the buffer is closed afterwards
        self.assertEqual(self.store.reserved, ['screenshot:' + ID] * 2)
        self.assertEqual(self.store.reservations, set())

    def test_system_waydroid_uses_the_same_path(self):
        self.assertEqual(self.save(self.query(png(1280, 720)), identifier='default')['runtime_id'], 'default')

    def test_buffer_is_anonymous_memory_closed_on_exec_and_sealed_before_the_copy(self):
        seen = {}

        def deliver(buffer, descriptor, size, owner=None):
            seen['flags'] = fcntl.fcntl(buffer, fcntl.F_GETFD)
            seen['seals'] = fcntl.fcntl(buffer, fcntl.F_GET_SEALS)
            seen['link'] = os.readlink(f'/proc/self/fd/{buffer}')
            seen['locked'] = self.store.mutex._is_owned()
            seen['descriptor'] = descriptor
            with self.assertRaises(OSError):
                os.pwrite(buffer, b'late', 0)
        with mock.patch.object(capture, 'deliver', deliver):
            self.save(self.query(png()))
        self.assertTrue(seen['flags'] & fcntl.FD_CLOEXEC)
        wanted = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
        self.assertEqual(seen['seals'] & wanted, wanted)
        self.assertIn('memfd:anvildroid-screenshot', seen['link'])
        self.assertFalse(seen['locked'])                        # slow file I/O never holds the controller lock
        self.assertEqual(seen['descriptor'], self.fd)

    def test_probe_runs_unlocked_before_android_is_asked(self):
        order = []

        def probe(descriptor, owner=None):
            order.append(('probe', descriptor, self.store.mutex._is_owned()))
            raise RuntimeError(capture.NOT_A_FILE)
        query = mock.Mock()
        with mock.patch.object(capture, 'probe', probe), self.assertRaisesRegex(RuntimeError, 'regular file'):
            self.save(query)
        self.assertEqual(order, [('probe', self.fd, False)])
        query.assert_not_called()
        self.assertEqual(self.store.reservations, set())

    def test_the_callers_file_is_only_touched_as_the_caller(self):
        owners = []
        data = png()
        with mock.patch.object(capture, 'probe', lambda descriptor, owner=None: owners.append(owner)), \
                mock.patch.object(capture, 'deliver', lambda buffer, descriptor, size, owner=None: owners.append(owner)):
            self.save(self.query(data))
        self.assertEqual(len(owners), 2)
        for owner in owners:
            self.assertEqual(owner[0], os.getuid())
            self.assertEqual(owner[1], os.getgid())
        self.assertIn('os.setgroups([])', capture.HELPER)
        self.assertLess(capture.HELPER.index('os.setuid(user)'), capture.HELPER.index("if mode == 'probe':"))

    def test_unusable_descriptors_are_refused_before_android_is_asked(self):
        query = mock.Mock()
        read_only = os.open(self.path, os.O_RDONLY)
        folder = os.open(self.folder.name, os.O_RDONLY | os.O_DIRECTORY)
        reader, writer = os.pipe()
        for fd in (read_only, folder, reader, writer):
            self.addCleanup(os.close, fd)
        for fd in (read_only, folder, reader, None, True, 'x', -1):
            with self.subTest(fd=fd), self.assertRaisesRegex(RuntimeError, 'opened for writing'):
                self.save(query, descriptor=fd)
        with self.assertRaisesRegex(RuntimeError, 'regular file'):
            self.save(query, descriptor=writer)
        query.assert_not_called()

    def test_invalid_captures_never_touch_the_file(self):
        data = png()
        self.path.write_bytes(b'keep')
        cases = [self.query(b''), self.query(data[:-1]), self.query(data[:4000]), self.query(b'JFIF' * 200),
                 self.query(png(0, 0)), self.query(png(70000, 1280)), self.query(data, claim=len(data) + 1),
                 self.query(data, claim=str(len(data))), lambda kind, params, sink: None]
        with mock.patch.object(capture, 'deliver') as deliver:
            for index, query in enumerate(cases):
                with self.subTest(index=index), self.assertRaisesRegex(RuntimeError, 'invalid screenshot'):
                    self.save(query)
            deliver.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'keep')

    def test_device_errors_pass_through_and_release_everything(self):
        def asleep(kind, params, sink=None):
            self.sinks.append(sink)
            raise RuntimeError('This device is asleep. Open an app or the Android window, then retry.')
        with self.assertRaisesRegex(RuntimeError, 'asleep'):
            self.save(asleep)
        with self.assertRaises(OSError):
            os.fstat(self.sinks[0])
        self.assertEqual(self.store.reservations, set())
        self.assertEqual(self.path.read_bytes(), b'')

    def test_failed_copy_is_reported_and_the_buffer_is_closed(self):
        def deliver(buffer, descriptor, size, owner=None):
            self.sinks.append(buffer)
            raise RuntimeError('Writing the screenshot took too long, so it was not saved. Choose another folder.')
        with mock.patch.object(capture, 'deliver', deliver), self.assertRaisesRegex(RuntimeError, 'took too long'):
            self.save(self.query(png()))
        with self.assertRaises(OSError):
            os.fstat(self.sinks[-1])
        self.assertEqual(self.store.reservations, set())

    def test_second_save_of_one_device_waits_its_turn(self):
        self.store.reservations.add('screenshot:' + ID)
        query = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'still being saved'):
            self.save(query)
        query.assert_not_called()

    def test_unknown_operation_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
            capture.dispatch(self.store, {'op': 'screen_record', 'id': ID}, os.getuid(), mock.Mock(), self.fd)


class Wiring(unittest.TestCase):
    def test_inspect_routes_the_descriptor_to_this_module(self):
        spec = importlib.util.spec_from_file_location('runtime_inspect', Path(__file__).with_name('runtime-inspect.py'))
        inspect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inspect)
        self.assertEqual(inspect.FIELDS['screenshot_save'], {'op', 'id'})
        self.assertIn('screencap', inspect.QUERIES)
        owner = inspect.OWNERS['screenshot_save']
        self.assertEqual(owner.DESCRIPTOR_OPS, {'screenshot_save'})
        with self.assertRaisesRegex(RuntimeError, 'needs a destination file'):
            inspect.dispatch(None, {'op': 'screenshot_save', 'id': 'default'}, os.getuid(), {})

    def test_operation_is_capped_as_a_slow_request(self):
        controller = Path(__file__).with_name('runtime-controller.py').read_text()
        block = controller[controller.index('SLOW_OPERATIONS = frozenset({'):]
        self.assertIn("'screenshot_save'", block[:block.index('})')])


if __name__ == '__main__':
    unittest.main()
