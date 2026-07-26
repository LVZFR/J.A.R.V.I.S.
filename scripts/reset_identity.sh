#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTH_DIR="${JARVIS_AUTH_DIR:-$ROOT/.jarvis-auth}"
if [[ "${1:-}" != "--yes" ]]; then
  printf 'This permanently removes the JARVIS passkey registration, face map, and voiceprint. Continue? [y/N] '
  read -r answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 1
fi
SERVICE="$(python3 - "$AUTH_DIR" <<'PY'
import hashlib, pathlib, sys
root = str(pathlib.Path(sys.argv[1]).expanduser().resolve())
print('com.nous.jarvis.biometric.' + hashlib.sha256(root.encode()).hexdigest()[:12])
PY
)"
if command -v security >/dev/null 2>&1; then
  security delete-generic-password -a "${USER:-jarvis}" -s "$SERVICE" >/dev/null 2>&1 || true
fi
rm -rf -- "$AUTH_DIR"
printf 'JARVIS identity enrollment reset. Restart JARVIS and enroll again at http://localhost:8787.\n'
