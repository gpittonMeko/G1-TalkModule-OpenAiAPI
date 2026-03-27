#!/bin/bash
# unitree_sdk2py root __init__.py imports b2 eagerly; that triggers a circular import on some setups.
# G1 (LocoClient, arm) does not need b2. Replace with minimal imports.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
SITE="$VENV/lib/python3.10/site-packages/unitree_sdk2py/__init__.py"
if [ ! -f "$SITE" ]; then
  echo "unitree_sdk2py not found at $SITE"
  exit 1
fi
cat > "$SITE" << 'EOF'
from . import idl, utils, core, rpc, go2

__all__ = [
    "idl",
    "utils",
    "core",
    "rpc",
    "go2",
]
EOF
echo "Patched $SITE (removed eager b2 import)."
