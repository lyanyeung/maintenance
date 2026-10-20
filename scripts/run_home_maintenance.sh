#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "=== Read-only audit ==="
python3 scripts/archive_home.py audit
echo "=== Verified copy to Research Filestore (NO DELETION) ==="
python3 scripts/archive_home.py backup
echo "=== Current quota ==="
if command -v myfiles >/dev/null 2>&1; then myfiles; fi
echo "To remove eligible old Home logs/archives, review the printed session and execute its explicit clean command."
