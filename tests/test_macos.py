import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from macos.backend import (Cancelled, NativeImages, copy_entries, image_size,
                           publish, scan, validate_output, MIB)


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.cancel = threading.Event()

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_unicode_empty_dirs_and_large_files(self):
        (self.source / '空 folder').mkdir()
        (self.source / 'empty').touch()
        (self.source / 'large.bin').write_bytes(os.urandom(5 * MIB))
        entries = scan(self.source, self.cancel)
        dest = self.root / 'copy'
        dest.mkdir()
        hashes = copy_entries(self.source, dest, entries, self.cancel, lambda *x: None)
        self.assertEqual(len(hashes), 2)
        self.assertTrue((dest / '空 folder').is_dir())
        self.assertEqual((dest / 'large.bin').read_bytes(), (self.source / 'large.bin').read_bytes())
        self.assertGreater(image_size(entries), 5 * MIB)

    def test_reject_links_and_case_collisions(self):
        (self.source / 'a').write_text('a')
        (self.source / 'link').symlink_to(self.source / 'a')
        with self.assertRaisesRegex(ValueError, 'Links'):
            scan(self.source, self.cancel)
        (self.source / 'link').unlink()
        # APFS on the native runner can be case insensitive.
        if not (self.source / 'A').exists():
            (self.source / 'A').write_text('b')
            with self.assertRaisesRegex(ValueError, 'collision'):
                scan(self.source, self.cancel)

    def test_invalid_names_and_modified_source(self):
        (self.source / 'invalid:name').touch()
        with self.assertRaisesRegex(ValueError, 'Invalid exFAT'):
            scan(self.source, self.cancel)
        (self.source / 'invalid:name').unlink()
        path = self.source / 'normal'
        path.write_text('before')
        entries = scan(self.source, self.cancel)
        path.write_text('after with different size')
        dest = self.root / 'copy'
        dest.mkdir()
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            copy_entries(self.source, dest, entries, self.cancel, lambda *x: None)

    def test_output_and_publish_never_overwrite(self):
        with self.assertRaises(ValueError):
            validate_output(self.source, self.source / 'output.exfat')
        output = self.root / 'existing.exfat'
        output.write_bytes(b'original')
        with self.assertRaises(FileExistsError):
            validate_output(self.source, output)
        stage = self.root / 'staged'
        stage.write_bytes(b'new')
        with self.assertRaises(OSError):
            publish(stage, output)
        self.assertEqual(output.read_bytes(), b'original')
        self.assertTrue(stage.exists())
        publish(stage, self.root / 'new.exfat')
        self.assertEqual((self.root / 'new.exfat').read_bytes(), b'new')

    def test_cancel_during_copy(self):
        (self.source / 'file').write_bytes(b'x' * (8 * MIB))
        entries = scan(self.source, self.cancel)
        dest = self.root / 'copy'
        dest.mkdir()
        def cancel_after_first_block(*args):
            self.cancel.set()
        with self.assertRaises(Cancelled):
            copy_entries(self.source, dest, entries, self.cancel, cancel_after_first_block)

    def test_format_guard_refuses_unrelated_disk(self):
        with patch('macos.backend.sys.platform', 'darwin'):
            backend = NativeImages()
        backend.run = lambda *args: plistlib.dumps({'images': [{
            'image-path': '/some/other/image.exfat',
            'system-entities': [{'dev-entry': '/dev/disk99'}]}]})
        with self.assertRaisesRegex(RuntimeError, 'Refusing to format'):
            backend.assert_backing_image('/dev/disk99', self.root / 'stage.exfat')

    def test_detach_on_failure(self):
        with patch('macos.backend.sys.platform', 'darwin'):
            backend = NativeImages()
        calls = []
        def run(*args):
            calls.append(args)
            return plistlib.dumps({'system-entities': [{'dev-entry': '/dev/disk99'}]})
        backend.run = run
        with self.assertRaisesRegex(ValueError, 'failed'):
            with backend.attached(self.root / 'stage.exfat'):
                raise ValueError('failed')
        self.assertEqual(calls[-1], ('/usr/bin/hdiutil', 'detach', '/dev/disk99'))


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('EXFAT_NATIVE_TEST') == '1',
                     'Native macOS disk image integration test')
class NativeRoundTrip(unittest.TestCase):
    def test_build_extract_and_raw_header(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'dump'
            source.mkdir()
            (source / 'eboot.bin').write_bytes(os.urandom(2 * MIB))
            (source / 'sce_sys').mkdir()
            (source / 'sce_sys/param.json').write_text('{"titleId":"PPSA00001"}')
            (source / '空 folder').mkdir()
            (source / 'empty').touch()
            backend = NativeImages(print)
            cancel = threading.Event()
            output = root / 'test.exfat'
            backend.build(source, output, cancel, lambda *x: None)
            with output.open('rb') as stream:
                self.assertEqual(stream.read(11)[3:11], b'EXFAT   ')
            dest = root / 'extracted'
            backend.extract(output, dest, cancel, lambda *x: None)
            for path in source.rglob('*'):
                copied = dest / path.relative_to(source)
                if path.is_dir():
                    self.assertTrue(copied.is_dir())
                else:
                    self.assertEqual(path.read_bytes(), copied.read_bytes())


@unittest.skipUnless(sys.platform == 'darwin', 'Tk Aqua smoke test requires macOS')
class DesktopSmoke(unittest.TestCase):
    def test_window_and_tabs(self):
        from macos.app import App
        app = App()
        try:
            app.update()
            self.assertEqual(app.status.get(), 'Ready')
            self.assertEqual(len(app.jobs), 0)
            self.assertGreater(app.winfo_width(), 700)
        finally:
            app.destroy()
