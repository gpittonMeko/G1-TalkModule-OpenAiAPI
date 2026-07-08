#!/bin/bash
# Occhi robot G1 → stream dashboard (percorso unico e sicuro sul Jetson).
#
# Cosa fa:
#   1. Verifica RealSense USB (8086:0b3a)
#   2. Installa pyrealsense2 nel .venv (compilato su Ubuntu 20.04, NO pip wheel)
#   3. Imposta G1_CAMERA_SOURCE=realsense in .env
#   4. Test frame + istruzioni restart
#
# Uso (sul Jetson):
#   cd ~/G1-TalkModule-OpenAiAPI
#   sed -i 's/\r$//' scripts/setup_robot_eyes.sh
#   bash scripts/setup_robot_eyes.sh
#
# Dashboard (PC sulla LAN robot):
#   https://192.168.123.164:8081/dashboard/  →  Avvia stream
#
# Sicurezza: lo stream esce solo da HTTPS :8081 (stesso server talk), non apre porte extra.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
PYTHON="$VENV/bin/python3"
ENV_FILE="$PROJECT_ROOT/.env"

cd "$PROJECT_ROOT"

echo "=============================================="
echo " G1 — setup occhi robot per dashboard"
echo "=============================================="

if ! lsusb 2>/dev/null | grep -qi '8086:0b3a\|Intel Corp'; then
  echo "ATTENZIONE: RealSense Intel non vista su USB."
  echo "  lsusb | grep -i intel"
  echo "Continuo comunque (camera potrebbe essere scollegata ora)."
fi

if "$PYTHON" -c "import pyrealsense2 as rs; print('pyrealsense2 già OK:', rs.__version__)" 2>/dev/null; then
  echo "== pyrealsense2 già funzionante nel venv =="
else
  echo "== Installazione pyrealsense2 (compilazione locale) =="
  bash "$PROJECT_ROOT/scripts/install_realsense_jetson.sh"
fi

echo "== Config .env =="
touch "$ENV_FILE"
# Ultima riga senza newline rompe RECORDING_TIMEOUT=10 → aggiungi sempre \n prima di append
[ -s "$ENV_FILE" ] && [ -z "$(tail -c1 "$ENV_FILE" | tr -d '\n')" ] || echo >> "$ENV_FILE"
if grep -q '^G1_CAMERA_SOURCE=' "$ENV_FILE"; then
  sed -i 's/^G1_CAMERA_SOURCE=.*/G1_CAMERA_SOURCE=realsense/' "$ENV_FILE"
else
  echo 'G1_CAMERA_SOURCE=realsense' >> "$ENV_FILE"
fi
# Ripara merge accidentale su RECORDING_TIMEOUT (es. 10G1_CAMERA_SOURCE=...)
if grep -q 'RECORDING_TIMEOUT=.*G1_CAMERA_SOURCE' "$ENV_FILE"; then
  sed -i 's/^RECORDING_TIMEOUT=\([0-9.]*\)G1_CAMERA_SOURCE=.*/RECORDING_TIMEOUT=\1\nG1_CAMERA_SOURCE=realsense/' "$ENV_FILE"
fi
if ! grep -q '^G1_YOLO_BACKEND=' "$ENV_FILE"; then
  echo 'G1_YOLO_BACKEND=onnx' >> "$ENV_FILE"
fi
if ! grep -q '^G1_CAMERA_YOLO=' "$ENV_FILE"; then
  echo 'G1_CAMERA_YOLO=1' >> "$ENV_FILE"
fi
echo "  G1_CAMERA_SOURCE=realsense"

echo "== Test frame occhi =="
"$PYTHON" << 'PY'
import pyrealsense2 as rs
import numpy as np

p = rs.pipeline()
c = rs.config()
c.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 15)
p.start(c)
frames = p.wait_for_frames(8000)
img = np.asanyarray(frames.get_color_frame().get_data())
print("OK frame:", img.shape)
p.stop()
PY

echo ""
echo "=============================================="
echo " PROSSIMO PASSO"
echo "=============================================="
echo "  bash scripts/restart_server.sh"
echo ""
echo " Poi dal PC (Ctrl+F5):"
echo "  https://192.168.123.164:8081/dashboard/"
echo "  → card Camera G1 + YOLO → Avvia stream"
echo ""
echo " Verifica API:"
echo "  curl -sk https://127.0.0.1:8081/api/camera/status"
echo "=============================================="
