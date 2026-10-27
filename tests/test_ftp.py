"""Real local FTP round trips; install pyftpdlib==2.1.0 to enable."""
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest

from macos.backend import download, ftp_connect, upload


@unittest.skipUnless(importlib.util.find_spec('pyftpdlib'), 'optional FTP test server not installed')
class FTPRoundTrip(unittest.TestCase):
    def setUp(self):
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote'
        self.remote.mkdir()
        authorizer = DummyAuthorizer()
        authorizer.add_user('test', 'local-test-only', str(self.remote), perm='elradfmwMT')
        class Handler(FTPHandler):
            pass
        Handler.authorizer = authorizer
        self.server = FTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'timeout': 0.05}, daemon=True)
        self.thread.start()
        self.cancel = threading.Event()

    def tearDown(self):
        self.server.close_all()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def connect(self):
        return ftp_connect('127.0.0.1', self.port, 'test', 'local-test-only')

    def test_upload_download_unicode_and_no_overwrite(self):
        data = bytes(range(256)) * 8192
        source = self.root / 'source'
        source.write_bytes(data)
        with self.connect() as ftp:
            upload(ftp, source, '/prueba á.exfat', self.cancel)
        self.assertEqual((self.remote / 'prueba á.exfat').read_bytes(), data)
        with self.connect() as ftp:
            with self.assertRaises(FileExistsError):
                upload(ftp, source, '/prueba á.exfat', self.cancel)
        destination = self.root / 'downloaded'
        with self.connect() as ftp:
            download(ftp, '/prueba á.exfat', destination, self.cancel)
        self.assertEqual(destination.read_bytes(), data)
        with self.connect() as ftp:
            with self.assertRaises(FileExistsError):
                download(ftp, '/prueba á.exfat', destination, self.cancel)

    def test_cancel_does_not_publish_download(self):
        (self.remote / 'large').write_bytes(b'x' * 4 * 1024 * 1024)
        destination = self.root / 'downloaded'
        def cancel(*args):
            self.cancel.set()
        with self.connect() as ftp:
            with self.assertRaisesRegex(RuntimeError, 'Partial download'):
                download(ftp, '/large', destination, self.cancel, cancel)
        self.assertFalse(destination.exists())
        self.assertTrue((self.root / 'downloaded.partial').exists())
