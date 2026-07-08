#!/bin/bash
# Build Intel librealsense + pyrealsense2 on Jetson (Ubuntu 20.04 / glibc 2.31).
# pip wheels need GLIBC >= 2.32 — compile against this system instead.
# Run on robot: bash scripts/install_realsense_jetson.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
PYTHON="${PYTHON:-$VENV/bin/python3}"
LIBRS_TAG="${LIBRS_TAG:-v2.54.2}"
LIBRS_SRC="${LIBRS_SRC:-$HOME/.cache/librealsense-src}"

if [ ! -x "$PYTHON" ]; then
  echo "Missing venv python: $PYTHON"
  exit 1
fi

echo "== System =="
ldd --version 2>&1 | head -1 || true
lsb_release -ds 2>/dev/null || true
lsusb | grep -i intel || echo "(no Intel USB device — collega RealSense prima del test)"

echo "== Remove incompatible pip wheel (GLIBC mismatch) =="
"$VENV/bin/pip" uninstall -y pyrealsense2 2>/dev/null || true

echo "== Build dependencies =="
# Repo apt Intel spesso presente sul Jetson con chiave GPG mancante; per build da sorgente non serve.
for f in /etc/apt/sources.list.d/*; do
  [ -f "$f" ] || continue
  case "$f" in
    *.disabled-by-g1-install|*.bak) continue ;;
  esac
  if grep -q 'librealsense.intel.com' "$f" 2>/dev/null; then
    sudo mv "$f" "${f}.disabled-by-g1-install"
    echo "  repo Intel apt disabilitato (non serve per compilazione): $f"
  fi
done
sudo apt-get update
sudo apt-get install -y \
  git cmake build-essential pkg-config \
  libssl-dev libusb-1.0-0-dev \
  libglfw3-dev libgtk-3-dev \
  python3-dev

if [ ! -d "$LIBRS_SRC/.git" ]; then
  echo "== Clone librealsense $LIBRS_TAG =="
  rm -rf "$LIBRS_SRC"
  git clone --depth 1 -b "$LIBRS_TAG" https://github.com/IntelRealSense/librealsense.git "$LIBRS_SRC"
fi

BUILD="$LIBRS_SRC/build"
rm -rf "$BUILD"
mkdir -p "$BUILD"

echo "== CMake (python=$PYTHON) =="
cmake -S "$LIBRS_SRC" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_EXAMPLES=OFF \
  -DBUILD_GRAPHICAL_EXAMPLES=OFF \
  -DBUILD_PYTHON_BINDINGS=ON \
  -DPYTHON_EXECUTABLE="$PYTHON" \
  -DFORCE_RSUSB_BACKEND=ON

if ! grep -q 'BUILD_PYTHON_BINDINGS:BOOL=ON' "$BUILD/CMakeCache.txt" 2>/dev/null; then
  echo "ERRORE: binding Python disabilitati. Serve python3-dev e riconfigurazione:"
  grep -E 'BUILD_PYTHON|PYTHON_' "$BUILD/CMakeCache.txt" 2>/dev/null || true
  exit 1
fi

echo "== Build (this can take 20–40 min on Jetson) =="
cmake --build "$BUILD" -j"$(nproc)"

echo "== Install libs + pyrealsense2 into venv =="
sudo cmake --install "$BUILD"
sudo ldconfig

install_pyrealsense2_to_venv() {
  local pkg init_py rel so
  rel="$BUILD/Release"
  pkg="$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])')/pyrealsense2"
  init_py="$LIBRS_SRC/wrappers/python/pyrealsense2/__init__.py"

  rm -rf "$pkg"
  mkdir -p "$pkg"

  if [ ! -d "$rel" ]; then
    echo "ERRORE: cartella build Release assente: $rel"
    find "$BUILD" -name '*.so' 2>/dev/null | head -20 || true
    exit 1
  fi

  for so in "$rel"/pyrealsense2.cpython-*.so "$rel"/pyrsutils.cpython-*.so "$rel"/pybackend2.cpython-*.so; do
    [ -f "$so" ] || continue
    cp -f "$so" "$pkg/"
    echo "  copiato $(basename "$so")"
  done

  if ! compgen -G "$pkg/pyrealsense2.cpython-"*.so >/dev/null; then
    echo "ERRORE: pyrealsense2.cpython-*.so non trovato in $rel"
    find "$BUILD" -name '*.so' 2>/dev/null | head -20 || true
    exit 1
  fi

  if [ -f "$init_py" ]; then
    cp -f "$init_py" "$pkg/"
  else
    printf '%s\n' 'from .pyrealsense2 import *' > "$pkg/__init__.py"
  fi
  echo "  pyrealsense2 installato in $pkg"
}

echo "== pyrealsense2 nel venv (sudo installa solo le .so di sistema) =="
install_pyrealsense2_to_venv

echo "== udev rules (accesso USB senza root) =="
sudo cp "$LIBRS_SRC/config/99-realsense-libusb.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "== Test occhi robot =="
"$PYTHON" << 'PY'
import pyrealsense2 as rs
import numpy as np

p = rs.pipeline()
c = rs.config()
c.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
p.start(c)
frames = p.wait_for_frames(5000)
img = np.asanyarray(frames.get_color_frame().get_data())
print("OK occhi robot:", img.shape, "pyrealsense2", rs.__version__)
p.stop()
PY

echo "Done. Set G1_CAMERA_SOURCE=realsense in .env then: bash scripts/restart_server.sh"
