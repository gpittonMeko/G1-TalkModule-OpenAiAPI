#!/bin/bash
# Install unitree_sdk2_python (+ cyclonedds 0.10.2) on Jetson (aarch64).
# PyPI has no linux_aarch64 wheel for cyclonedds 0.10.2 — build C lib once, then pip.
# Run on robot: bash scripts/install_unitree_sdk_jetson.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
CYCLONE_SRC="${CYCLONE_SRC:-$HOME/.cache/cyclonedds-0.10-src}"
INSTALL_PREFIX="${CYCLONEDDS_INSTALL:-$HOME/cyclonedds-install}"

if [ ! -x "$VENV/bin/pip" ]; then
  echo "Missing venv: $VENV"
  exit 1
fi

echo "== Disk (before) =="
df -h / /home 2>/dev/null | head -5

if [ ! -f "$INSTALL_PREFIX/lib/libddsc.so" ] && [ ! -f "$INSTALL_PREFIX/lib/aarch64-linux-gnu/libddsc.so" ]; then
  echo "== Building Cyclone DDS C library into $INSTALL_PREFIX =="
  rm -rf "$CYCLONE_SRC"
  mkdir -p "$CYCLONE_SRC"
  git clone --depth 1 -b releases/0.10.x https://github.com/eclipse-cyclonedds/cyclonedds.git "$CYCLONE_SRC"
  cmake -S "$CYCLONE_SRC" -B "$CYCLONE_SRC/build" \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_EXAMPLES=OFF \
    -DBUILD_TESTING=OFF \
    -DENABLE_SECURITY=OFF
  cmake --build "$CYCLONE_SRC/build" -j"$(nproc)"
  cmake --install "$CYCLONE_SRC/build"
  rm -rf "$CYCLONE_SRC/build"
  echo "== Removed build tree; sources kept at $CYCLONE_SRC (delete with rm -rf to free ~50MB+) =="
else
  echo "== Cyclone DDS already present under $INSTALL_PREFIX =="
fi

export CYCLONEDDS_HOME="$INSTALL_PREFIX"
export CMAKE_PREFIX_PATH="$INSTALL_PREFIX:${CMAKE_PREFIX_PATH:-}"

echo "== pip: cyclonedds + unitree_sdk2py (from git; wheel omits G1 without __init__.py) =="
"$VENV/bin/pip" install --no-cache-dir cyclonedds==0.10.2
SDK_SRC="${UNITREE_SDK2_SRC:-$HOME/.cache/unitree_sdk2_python-src}"
rm -rf "$SDK_SRC"
git clone --depth 1 https://github.com/unitreerobotics/unitree_sdk2_python.git "$SDK_SRC"
# setuptools find_packages skips g1/* (no __init__.py upstream) — add namespace markers
for d in g1 g1/arm g1/loco g1/audio; do
  touch "$SDK_SRC/unitree_sdk2py/$d/__init__.py"
done
(
  cd "$SDK_SRC"
  export CYCLONEDDS_HOME="$INSTALL_PREFIX"
  export CMAKE_PREFIX_PATH="$INSTALL_PREFIX:${CMAKE_PREFIX_PATH:-}"
  "$VENV/bin/pip" install --no-cache-dir .
)
rm -rf "$SDK_SRC"

echo "== Patch package __init__ (avoid b2 circular import; G1-only) =="
bash "$PROJECT_ROOT/scripts/patch_unitree_sdk2py_init.sh"

echo "== Verify LocoClient =="
"$VENV/bin/python3" -c "from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient; print('LocoClient import OK')"

echo "== Disk (after) =="
df -h / /home 2>/dev/null | head -5
echo "Done. Restart talk server: bash scripts/restart_server.sh"
