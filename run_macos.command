#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
PYTHON="${EXFAT_PYTHON:-python3}"
if ! "$PYTHON" -c 'import tkinter' 2>/dev/null; then
  echo 'Python with Tk is required. Install Python from https://www.python.org/downloads/macos/'
  echo 'With Homebrew: brew install python@3.12 python-tk@3.12'
  echo 'Then set EXFAT_PYTHON to the matching Python executable.'
  read -r -p 'Press Return to close.'
  exit 1
fi
exec "$PYTHON" macos_app.py
