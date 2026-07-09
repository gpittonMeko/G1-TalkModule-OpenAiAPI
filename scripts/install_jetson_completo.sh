#!/bin/bash
# Installazione COMPLETA su Jetson G1 (nuovo robot o reinstall).
# Include: Python/venv, Talk Module, OpenCV/YOLO, Unitree SDK2 (movimenti DDS).
#
# Uso sul Jetson:
#   cd ~/G1-TalkModule-OpenAiAPI
#   bash scripts/install_jetson_completo.sh
#
# Opzioni:
#   --realsense   Installa anche camera RealSense (occhi robot, lungo)
#   --skip-sdk    Salta unitree_sdk2 (solo voce/camera, niente braccia/loco)
#   --skip-camera Salta OpenCV/YOLO

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

WITH_REALSENSE=0
SKIP_SDK=0
SKIP_CAMERA=0
for arg in "$@"; do
  case "$arg" in
    --realsense) WITH_REALSENSE=1 ;;
    --skip-sdk) SKIP_SDK=1 ;;
    --skip-camera) SKIP_CAMERA=1 ;;
  esac
done

echo "=============================================="
echo "  G1 Talk — installazione Jetson COMPLETA"
echo "  $PROJECT_ROOT"
echo "=============================================="
df -h "$PROJECT_ROOT" / 2>/dev/null | head -5 || true
echo ""

echo "[1/6] Base Talk Module (Python 3.10 + venv + requirements.txt)..."
bash "$SCRIPT_DIR/install_jetson_tutto_in_cartella.sh"

if [ "$SKIP_CAMERA" -eq 0 ]; then
  echo ""
  echo "[2/6] Dipendenze sistema per camera/OpenCV..."
  if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
      v4l-utils libglib2.0-0 libgomp1 \
      cmake build-essential git pkg-config
  fi
  echo "[2/6] pip: requirements-camera.txt (OpenCV headless)..."
  "$PROJECT_ROOT/.venv/bin/pip" install -q -r "$PROJECT_ROOT/requirements-camera.txt"
  echo "[2/6] Download modello YOLO ONNX..."
  bash "$SCRIPT_DIR/download_yolov8n_onnx.sh"
else
  echo "[2/6] Camera/YOLO saltato (--skip-camera)"
fi

if [ "$SKIP_SDK" -eq 0 ]; then
  echo ""
  echo "[3/6] Unitree SDK2 (Cyclone DDS + unitree_sdk2py — braccia/loco/audio robot)..."
  bash "$SCRIPT_DIR/install_unitree_sdk_jetson.sh"
else
  echo "[3/6] SDK Unitree saltato (--skip-sdk)"
fi

if [ "$WITH_REALSENSE" -eq 1 ]; then
  echo ""
  echo "[4/6] RealSense (camera integrata G1)..."
  bash "$SCRIPT_DIR/install_realsense_jetson.sh"
  bash "$SCRIPT_DIR/setup_robot_eyes.sh" || true
else
  echo "[4/6] RealSense saltato (aggiungi --realsense se serve camera occhi robot)"
fi

echo ""
echo "[5/6] Certificato HTTPS (se .env ha TALK_PUBLIC_HOST)..."
if [ -f "$PROJECT_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^TALK_PUBLIC_HOST=' "$PROJECT_ROOT/.env" 2>/dev/null | sed 's/\r$//') || true
  set +a
  if [ -n "${TALK_PUBLIC_HOST:-}" ]; then
    TALK_PUBLIC_HOST="$TALK_PUBLIC_HOST" bash "$SCRIPT_DIR/generate_ssl_cert.sh" || true
  else
    echo "      Salta: imposta TALK_PUBLIC_HOST in .env poi: bash scripts/generate_ssl_cert.sh <IP>"
  fi
else
  echo "      Salta: cp .env.example .env e imposta TALK_PUBLIC_HOST"
fi

echo ""
echo "[6/6] Verifica dipendenze..."
"$PROJECT_ROOT/.venv/bin/python3" "$SCRIPT_DIR/verify_jetson_deps.py" || true

echo ""
echo "=============================================="
echo "  Installazione completata"
echo "=============================================="
echo ""
echo "  Prossimi passi:"
echo "  1. nano .env   # OPENAI_API_KEY, TALK_PUBLIC_HOST, UNITREE_DDS_INTERFACE"
echo "  2. bash scripts/restart_server.sh"
echo "  3. bash scripts/diagnose_g1_robot.py   # test DDS + SDK"
echo "  4. Browser: https://<IP-JETSON>:8081/client"
echo ""
echo "  Documentazione: docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md"
echo ""
