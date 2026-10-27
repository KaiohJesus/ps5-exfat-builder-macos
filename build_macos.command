#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ -z "${EXFAT_PYTHON:-}" ]] && command -v brew >/dev/null 2>&1; then
  PREFIX="$(brew --prefix python@3.12 2>/dev/null || true)"
  if [[ -x "$PREFIX/bin/python3.12" ]]; then
    export EXFAT_PYTHON="$PREFIX/bin/python3.12"
  fi
fi
if ./build_macos.sh; then
  open release
else
  echo 'Build failed. Copy the terminal error when reporting the issue.'
fi
read -r -p 'Press Return to close.'
