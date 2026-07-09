#!/bin/bash
# Esegui SUL JETSON da ~/G1-TalkModule-OpenAiAPI (una volta):
#   bash scripts/install_jetson_tutto_in_cartella.sh
#
# - Controlla spazio disco sulla partizione della cartella progetto
# - Installa Python 3.10 di sistema (serve sudo apt) se manca
# - Crea SOLO .venv dentro questa cartella (nessun venv altrove)
# - Poi: install.sh --no-audio

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=============================================="
echo "  G1 Talk — spazio disco (cartella progetto)"
echo "=============================================="
df -h "$PROJECT_ROOT"
echo ""
df -h / 2>/dev/null || true
echo ""

# Tutto il progetto (codice + venv + temp) resta sotto PROJECT_ROOT
export PYTHON="${PYTHON:-}"
if command -v python3.10 &>/dev/null; then
  PYTHON="python3.10"
  echo "[OK] Trovato $PYTHON — $($PYTHON --version)"
elif [ -n "$PYTHON" ] && command -v "$PYTHON" &>/dev/null && "$PYTHON" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
  echo "[OK] Uso PYTHON=$PYTHON — $($PYTHON --version)"
else
  echo "Python 3.10+ non trovato. Installazione pacchetti di sistema (richiede sudo)..."
  if ! command -v sudo &>/dev/null; then
    echo "ERRORE: serve sudo per apt. Esegui come utente con privilegi sudo."
    exit 1
  fi
  sudo apt-get update -qq
  sudo apt-get install -y -qq software-properties-common
  if sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.10 python3.10-venv python3.10-dev
  else
    echo "PPA deadsnakes non disponibile su questa immagine. Installa Python 3.10 manualmente"
    echo "(docs/INSTALLAZIONE.md) e rilancia: PYTHON=python3.10 bash $0"
    exit 1
  fi
  PYTHON="python3.10"
  echo "[OK] Installato $PYTHON — $($PYTHON --version)"
fi

echo ""
echo "=============================================="
echo "  Installazione dipendenze in SOLO questa cartella"
echo "  (venv -> $PROJECT_ROOT/.venv)"
echo "=============================================="
PYTHON="$PYTHON" bash "$PROJECT_ROOT/install.sh" --no-audio

echo ""
echo "Prossimi passi (sempre da $PROJECT_ROOT):"
echo "  # Installazione COMPLETA nuovo G1 (SDK + OpenCV/YOLO):"
echo "  bash scripts/install_jetson_completo.sh"
echo ""
echo "  # Oppure manualmente dopo questo script:"
echo "  pip install -r requirements-camera.txt"
echo "  bash scripts/install_unitree_sdk_jetson.sh"
echo "  bash scripts/download_yolov8n_onnx.sh"
echo ""
echo "  cp .env.example .env && nano .env   # OPENAI_API_KEY, TALK_PUBLIC_HOST"
echo "  TALK_PUBLIC_HOST=<IP> bash scripts/generate_ssl_cert.sh"
echo "  .venv/bin/python3 scripts/verify_jetson_deps.py"
echo "  bash scripts/restart_server.sh"
echo ""
echo "  Documentazione: docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md"
echo ""
