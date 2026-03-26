#!/bin/bash
# Sul Jetson: crea .venv nella cartella progetto e pip install SENZA sudo.
# Uso: cd ~/G1-TalkModule-OpenAiAPI && bash scripts/bootstrap_venv_no_sudo.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3.10}"
if ! command -v "$PY" &>/dev/null; then
  echo "ERRORE: $PY non trovato. Installa python3.10 (sudo apt ...) o imposta PYTHON="
  exit 1
fi

echo "Uso: $PY — $($PY --version)"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
python -c "from talk_module.web_app import app; print('Modulo OK')"
echo ""
echo "Fatto. Poi: bash scripts/restart_server.sh"
