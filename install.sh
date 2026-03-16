#!/bin/bash
# G1 Talk Module - Installazione su Linux (Ubuntu/Debian, Jetson, Raspberry Pi)
# Uso: bash install.sh [--no-audio]
#   --no-audio  Salta installazione PortAudio (solo dispositivi web/ rete)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=============================================="
echo "  G1 Talk Module - Installazione"
echo "=============================================="
echo ""

# Python
if ! command -v python3 &>/dev/null; then
    echo "ERRORE: Python 3 non trovato. Installa con: sudo apt install python3 python3-venv"
    exit 1
fi
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[1/6] Python $PYVER: OK"

# Dipendenze sistema
echo ""
echo "[2/6] Dipendenze sistema..."
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-dev python3-venv ffmpeg
    if [[ "$*" != *"--no-audio"* ]]; then
        sudo apt-get install -y -qq portaudio19-dev libsndfile1 libsndfile1-dev
        echo "      PortAudio + FFmpeg: OK"
    else
        echo "      FFmpeg: OK (PortAudio saltato - solo modalita rete)"
    fi
else
    echo "      Avviso: apt non trovato. Installa manualmente: python3-venv, ffmpeg"
    if [[ "$*" != *"--no-audio"* ]]; then
        echo "      E anche: portaudio19-dev, libsndfile1"
    fi
fi

# Virtual environment
echo ""
echo "[3/6] Virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "      Creato .venv"
else
    echo "      .venv esistente"
fi
source .venv/bin/activate

# Pip
echo ""
echo "[4/6] Dipendenze Python..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "      OK"

# Config
echo ""
echo "[5/6] Configurazione..."
NEED_KEY=false
if [ ! -f ".env" ]; then
    cp .env.example .env
    NEED_KEY=true
elif ! grep -q "OPENAI_API_KEY=sk-" .env 2>/dev/null; then
    NEED_KEY=true
fi

if [ "$NEED_KEY" = "true" ]; then
    echo "      Avvio installer grafico..."
    echo ""
    (python3 -m installer.main 2>/dev/null) || {
        echo "      Fallback: modifica .env e inserisci OPENAI_API_KEY"
        echo "      nano .env"
    }
else
    echo "      .env gia configurato"
fi

# Script restart (fix CRLF se presente)
if [ -f "scripts/restart_server.sh" ]; then
    sed -i 's/\r$//' scripts/restart_server.sh 2>/dev/null || true
    chmod +x scripts/restart_server.sh
fi

echo ""
echo "[6/6] Verifica..."
python3 -c "from talk_module.web_app import app; print('      Modulo OK')" 2>/dev/null || {
    echo "      Avviso: verifica manuale con: python3 -m talk_module.web_app --help"
}

echo ""
echo "=============================================="
echo "  Installazione completata"
echo "=============================================="
echo ""
echo "  Prossimi passi:"
echo "  1. Modifica .env e inserisci OPENAI_API_KEY"
echo "  2. bash scripts/restart_server.sh"
echo "  3. Apri: http://<IP>:8081/client"
echo ""
echo "  Leggi: LEGGIMI.txt"
echo ""
