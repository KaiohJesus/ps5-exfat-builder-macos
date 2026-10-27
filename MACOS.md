# exFAT Image Builder — macOS edition

A native macOS front end in the fork of Deckerr97's exFAT Image Builder.
Upstream reference: v4.0.2. The original Windows application is preserved.

## Run

Use a Python installation with Tk, such as the macOS installer from
https://www.python.org/downloads/macos/ . No pip dependencies are required to
run the native app from source.

```sh
python3 macos_app.py
```

Alternatively double-click `run_macos.command`. With Homebrew:

```sh
brew install python@3.12 python-tk@3.12
EXFAT_PYTHON="$(brew --prefix python@3.12)/bin/python3.12" ./run_macos.command
```

## Features and current scope

| Feature | macOS edition |
| --- | --- |
| Raw `.exfat` creation | Native hdiutil/diskutil; no Windows tools |
| Multiple queued dumps | Sequential build queue with cancellation |
| Verification | Read-only remount, SHA-256 comparison for every copied file |
| `.exfat` extraction | Read-only input; new destination; hash verification |
| FTP | Folder navigation, upload, download, size verification |
| Dump library | Scan folders, read param.json, select a source |
| FFPKG / PFS compression | Not ported |
| Backport / DLC editor / AMPR | Not ported |
| Payload / klog / mount configuration panels | Not ported |
| Windows application | Unchanged, run `exfat_builder.py` on Windows |

This is an initial native edition, not a claim of complete Windows feature
parity. Native integration checks must pass before a build is considered usable;
console boot compatibility still requires testing on PS5 hardware.

## Build a standalone .app

```sh
./build_macos.sh
```

The `.app` includes Python and Tk. Build once on Apple Silicon and once on Intel;
the workflow builds both architectures on macOS 15 runners. Outputs are ZIP files
and SHA-256 checksums in `release/`, also available in GitHub Actions artifacts.
No paid Apple Developer ID or notarization credentials are configured; these
are development builds, not notarized releases. Use Apple's normal approval
flow for software you trust. Do not disable system security settings.

## Using the app

1. Select a dump folder containing `eboot.bin`.
2. Choose a **new** `.exfat` output path outside the source folder.
3. Add jobs to the queue and start the build.
4. Wait for formatting, copying and read-only verification to finish.
5. For extraction, choose the image and name a **new** destination folder.

The output is a raw exFAT filesystem with the boot sector at offset zero,
not a DMG container or a partitioned disk image. The app formats only a
new temporary backing image and verifies its association with the virtual
`/dev/diskN` through `hdiutil info` before formatting. It never accepts a
physical disk identifier from the user. It does not request root at launch.

Reserve image-size free space on the output filesystem. Image sizing includes
cluster-rounding and filesystem metadata allowance. Output files are published
without overwriting existing paths. The output filesystem must support exclusive
rename (`renamex_np`); unsupported filesystems produce a visible error and retain
the temporary image. On cancel/error, temporary images and partial extractions
are retained with their paths in the error/log for manual recovery. Never delete
a backing image that is still mounted; eject it in Disk Utility first.

Symlinks, special files, exFAT-invalid names and case-insensitive filename
collisions are rejected. Changes detected in a source file during copying abort
the build. Finder metadata directories are excluded. Do not modify the source
during a build. Native format operations finish before cancellation takes effect.

FTP is plaintext and intended for a trusted local network. Credentials are kept
only in memory. Uploads use unique `.partial` names and verify server-reported
size before rename. Interrupted transfers can leave a temporary file on the PS5.
FTP size verification is not a remote cryptographic integrity check.

## Validation

```sh
python3 -m unittest discover -s tests -v
# On macOS only: creates a small temporary virtual image, never a physical disk.
EXFAT_NATIVE_TEST=1 python3 -m unittest discover -s tests -v
```

The native test builds and verifies a raw image, detaches it, reattaches it
read-only, extracts it, then compares bytes including Unicode paths, empty
files and empty directories. The build workflow requires this round trip before
packaging the application.

## Credits

Original exFAT Image Builder: Deckerr97 / kerrdec97,
https://github.com/kerrdec97/ps5-exfat-builder . Preserve all upstream notices in
`THIRD_PARTY_NOTICES.md`. The macOS entry point does not execute or bundle the
embedded Windows executables or PS5 payloads. This tool is for personal backups
of legally owned content and is not affiliated with Sony.
