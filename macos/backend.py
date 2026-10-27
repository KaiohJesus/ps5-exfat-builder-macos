"""Native raw exFAT images. Never accepts a physical disk as a format target."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ftplib
import ctypes
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading

MIB = 1024 * 1024
SYSTEM_NAMES = {'.DS_Store', '.Spotlight-V100', '.Trashes', '.fseventsd'}


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    relative: Path
    size: int
    mtime_ns: int
    directory: bool = False


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled('Operation cancelled')


def scan(source: Path, cancel: threading.Event) -> list[Entry]:
    """Reject links, special files and names exFAT cannot represent."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError('Select a source folder')
    entries = []
    names = set()
    for root, dirs, files in os.walk(source, followlinks=False):
        check_cancel(cancel)
        dirs[:] = sorted(n for n in dirs if n not in SYSTEM_NAMES)
        for name in dirs + sorted(n for n in files if n not in SYSTEM_NAMES):
            check_cancel(cancel)
            path = Path(root) / name
            rel = path.relative_to(source)
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError(f'Links and special files are unsupported: {rel}')
            if re.search(r'[\x00-\x1f<>:"/\\|?*]', name) or name.endswith((' ', '.')):
                raise ValueError(f'Invalid exFAT name: {rel}')
            if len(name.encode('utf-16-le')) > 510:
                raise ValueError(f'Name too long for exFAT: {rel}')
            key = rel.as_posix().casefold()
            if key in names:
                raise ValueError(f'Case-insensitive filename collision: {rel}')
            names.add(key)
            st = path.stat()
            entries.append(Entry(rel, st.st_size, st.st_mtime_ns, path.is_dir()))
    return entries


def image_size(entries):
    # Reserve cluster-rounding slack per file plus filesystem metadata.
    total = sum(((e.size + MIB - 1) // MIB) * MIB if not e.directory else MIB
                for e in entries)
    return max(256 * MIB, ((total * 110 // 100 + 256 * MIB + MIB - 1) // MIB) * MIB)


def validate_output(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError('Output must be outside the source folder')
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}')
    if not output.parent.is_dir():
        raise ValueError('Output folder does not exist')
    if output.suffix.lower() != '.exfat':
        raise ValueError('Output filename must end in .exfat')
    return source, output


def sha256(path, cancel):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            check_cancel(cancel)
            block = stream.read(4 * MIB)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def copy_entries(source, dest, entries, cancel, progress):
    hashes = {}
    total = sum(e.size for e in entries if not e.directory)
    done = 0
    for e in entries:
        check_cancel(cancel)
        src, target = source / e.relative, dest / e.relative
        if src.is_symlink():
            raise ValueError(f'Source changed into a link: {e.relative}')
        if e.directory:
            target.mkdir(parents=True, exist_ok=True)
            continue
        before = src.stat()
        if (before.st_size, before.st_mtime_ns) != (e.size, e.mtime_ns):
            raise ValueError(f'Source changed since scan: {e.relative}')
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with src.open('rb') as reader, target.open('xb') as writer:
            while True:
                check_cancel(cancel)
                block = reader.read(4 * MIB)
                if not block:
                    break
                writer.write(block)
                digest.update(block)
                done += len(block)
                progress('Copy', done, max(total, 1), str(e.relative))
            writer.flush()
            os.fsync(writer.fileno())
        after = src.stat()
        if (after.st_size, after.st_mtime_ns) != (e.size, e.mtime_ns):
            raise ValueError(f'Source changed during copy: {e.relative}')
        hashes[e.relative] = digest.hexdigest()
    return hashes


class NativeImages:
    def __init__(self, log=lambda text: None):
        if sys.platform != 'darwin':
            raise RuntimeError('Native image operations require macOS')
        self.log = log

    def run(self, *args):
        # No shell, sudo or user-provided disk nodes. Do not interrupt a format
        # command midway; cancellation is checked between native operations.
        self.log(' '.join(map(str, args)))
        result = subprocess.run(list(map(str, args)), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip()
                               or f'Command failed: {args[0]}')
        return result.stdout

    def detach(self, device):
        # Retain the image on detach failure so a mounted backing file is never
        # unlinked; caller reports the exact device for manual cleanup.
        self.run('/usr/bin/hdiutil', 'detach', device)

    @contextmanager
    def attached(self, image, readonly=False):
        args = ['/usr/bin/hdiutil', 'attach', '-nomount', '-plist',
                '-imagekey', 'diskimage-class=CRawDiskImage']
        if readonly:
            args.append('-readonly')
        data = plistlib.loads(self.run(*args, image))
        devices = [e['dev-entry'] for e in data.get('system-entities', [])
                   if re.fullmatch(r'/dev/disk\d+', e.get('dev-entry', ''))]
        if len(devices) != 1:
            raise RuntimeError('Could not identify attached image device; image retained')
        device = devices[0]
        try:
            yield device
        finally:
            self.detach(device)

    def assert_backing_image(self, device, image):
        """Verify the disk node belongs to this exact temporary image."""
        info = plistlib.loads(self.run('/usr/bin/hdiutil', 'info', '-plist'))
        for item in info.get('images', []):
            backing = item.get('image-path', '')
            if backing and Path(backing).resolve() == image.resolve():
                entities = item.get('system-entities', [])
                if any(e.get('dev-entry') == device for e in entities):
                    return
        raise RuntimeError('Refusing to format: disk is not backed by the temporary image')

    @contextmanager
    def mounted(self, device):
        # Custom short path avoids collisions with existing PS5DATA volumes.
        with tempfile.TemporaryDirectory(prefix='exfat-mount-') as folder:
            self.run('/usr/sbin/diskutil', 'mount', '-mountPoint', folder, device)
            try:
                info = plistlib.loads(self.run('/usr/sbin/diskutil', 'info', '-plist', device))
                if Path(info.get('MountPoint', '')).resolve() != Path(folder).resolve():
                    raise RuntimeError('Unexpected mount point')
                yield Path(folder)
            finally:
                self.run('/usr/sbin/diskutil', 'unmount', device)

    def build(self, source, output, cancel, progress=lambda *args: None):
        source, output = validate_output(Path(source), Path(output))
        progress('Scan', 0, 1, str(source))
        entries = scan(source, cancel)
        size = image_size(entries)
        if shutil.disk_usage(output.parent).free < size + 64 * MIB:
            raise ValueError('Not enough free space for the image and metadata')
        check_cancel(cancel)
        fd, name = tempfile.mkstemp(prefix='.exfat-build-', suffix='.exfat', dir=output.parent)
        stage = Path(name)
        # Never remove an image on error: detach may have failed. It remains
        # available for diagnosis/recovery, rather than deleting a mounted file.
        with os.fdopen(fd, 'wb') as stream:
            stream.truncate(size)
        self.log(f'Temporary image: {stage}')
        try:
            with self.attached(stage) as device:
                self.assert_backing_image(device, stage)
                check_cancel(cancel)
                progress('Format', 0, 1, 'Creating raw exFAT filesystem')
                self.run('/usr/sbin/diskutil', 'eraseVolume', 'ExFAT', 'PS5DATA', device)
                # eraseVolume auto-mounts; move to our own controlled mountpoint.
                self.run('/usr/sbin/diskutil', 'unmount', device)
                with self.mounted(device) as mount:
                    hashes = copy_entries(source, mount, entries, cancel, progress)
            # Verify after detach and read-only reattach, not against write cache.
            with self.attached(stage, readonly=True) as device:
                with self.mounted(device) as mount:
                    for index, entry in enumerate(entries):
                        check_cancel(cancel)
                        target = mount / entry.relative
                        if entry.directory:
                            if not target.is_dir():
                                raise ValueError(f'Missing directory: {entry.relative}')
                        elif (not target.is_file() or target.stat().st_size != entry.size
                              or sha256(target, cancel) != hashes[entry.relative]):
                            raise ValueError(f'Image verification failed: {entry.relative}')
                        progress('Verify', index + 1, max(len(entries), 1), str(entry.relative))
            # A raw exFAT superfloppy must have its boot sector at byte zero.
            with stage.open('rb') as stream:
                header = stream.read(512)
            if header[3:11] != b'EXFAT   ' or header[510:512] != b'\x55\xaa':
                raise ValueError('Output is not a raw exFAT filesystem')
            check_cancel(cancel)
            # Atomic no-clobber publication on the same filesystem.
            publish(stage, output)
            progress('Complete', 1, 1, str(output))
            return output
        except Exception as exc:
            raise RuntimeError(f'{exc}\nTemporary image retained: {stage}') from exc

    def extract(self, image, destination, cancel, progress=lambda *args: None):
        image, destination = Path(image).resolve(strict=True), Path(destination).resolve()
        if not image.is_file() or destination.exists() or not destination.parent.is_dir():
            raise ValueError('Select an image and a NEW destination folder')
        destination.mkdir()
        try:
            with self.attached(image, readonly=True) as device:
                with self.mounted(device) as mount:
                    entries = scan(mount, cancel)
                    hashes = copy_entries(mount, destination, entries, cancel, progress)
                    for index, (relative, expected) in enumerate(hashes.items()):
                        if sha256(destination / relative, cancel) != expected:
                            raise ValueError(f'Extraction verification failed: {relative}')
                        progress('Verify', index + 1, max(len(hashes), 1), str(relative))
            progress('Complete', 1, 1, str(destination))
        except Exception:
            self.log(f'Partial extraction retained: {destination}')
            raise


def publish(source: Path, destination: Path):
    """Publish without replacing an existing file, including on exFAT drives."""
    if sys.platform == 'darwin':
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 4) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(destination))
    else:
        # Portable path used only by non-native unit tests.
        os.link(source, destination)
        source.unlink()


def ftp_connect(host, port, username='anonymous', password=''):
    if not host.strip() or not 1 <= int(port) <= 65535:
        raise ValueError('Enter a valid host and port')
    ftp = ftplib.FTP(timeout=30)
    try:
        ftp.connect(host.strip(), int(port))
        ftp.login(username or 'anonymous', password)
        ftp.encoding = 'utf-8'
        return ftp
    except Exception:
        ftp.close()
        raise


def upload(ftp, local, remote, cancel, progress=lambda *args: None):
    """Upload to a unique temporary name; publish without replacing a file."""
    import uuid
    if '\r' in remote or '\n' in remote or not remote or remote.endswith('/'):
        raise ValueError('Enter a full remote filename')
    parent, _, name = remote.rpartition('/')
    ftp.cwd(parent or '.')
    if name in {Path(n).name for n in ftp.nlst()}:
        raise FileExistsError(f'Remote file exists: {name}')
    temp = f'.{name}.{uuid.uuid4().hex}.partial'
    size = Path(local).stat().st_size
    done = 0
    def tick(block):
        nonlocal done
        check_cancel(cancel)
        done += len(block)
        progress('FTP upload', done, max(size, 1), name)
    try:
        with open(local, 'rb') as stream:
            ftp.storbinary(f'STOR {temp}', stream, blocksize=256 * 1024, callback=tick)
        check_cancel(cancel)
        ftp.voidcmd('TYPE I')
        if ftp.size(temp) != size:
            raise ValueError('Remote size does not match local file')
        if name in {Path(n).name for n in ftp.nlst()}:
            raise FileExistsError(f'Remote file appeared during upload: {name}')
        ftp.rename(temp, name)
    except Exception:
        # Callback exceptions leave FTP control replies unsynchronized. Closing
        # is safer than issuing commands that could target the wrong transfer.
        ftp.close()
        raise RuntimeError(f'Upload incomplete; remote temporary file may remain: {temp}')
