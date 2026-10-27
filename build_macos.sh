#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Build this application on macOS.' >&2
  exit 1
fi
PYTHON="${EXFAT_PYTHON:-python3}"
"$PYTHON" -c 'import tkinter' || { echo 'Install a Python distribution with Tk support.' >&2; exit 1; }
"$PYTHON" -m venv .venv-macos
.venv-macos/bin/python -m pip install -r requirements-macos-build.txt
.venv-macos/bin/python -m unittest discover -s tests -v
.venv-macos/bin/python -m PyInstaller --clean --noconfirm --windowed --onedir \
  --name 'exFAT Image Builder macOS' --osx-bundle-identifier com.kaiohjesus.exfatbuilder \
  --add-data 'THIRD_PARTY_NOTICES.md:.' --add-data 'MACOS.md:.' macos_app.py
# No Developer ID credentials are embedded. PyInstaller applies an ad-hoc signature.
ARCH="$(uname -m)"
mkdir -p release
/usr/bin/ditto -c -k --sequesterRsrc --keepParent \
  'dist/exFAT Image Builder macOS.app' "release/exFAT-Image-Builder-macOS-${ARCH}.zip"
(cd release && shasum -a 256 "exFAT-Image-Builder-macOS-${ARCH}.zip" > "exFAT-Image-Builder-macOS-${ARCH}.zip.sha256")
echo "Built release/exFAT-Image-Builder-macOS-${ARCH}.zip"
