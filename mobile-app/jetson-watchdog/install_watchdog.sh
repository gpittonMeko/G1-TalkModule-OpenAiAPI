#!/bin/bash
# =============================================================================
# install_watchdog.sh  –  Installa talk-watchdog come servizio systemd sulla Jetson
#
# Uso:
#   bash install_watchdog.sh [/percorso/repo]
#
# Il primo argomento (opzionale) è la root del progetto G1-TalkModule-OpenAiAPI.
# Se omesso, viene rilevata automaticamente dalla posizione dello script.
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -n "$1" ]; then
    PROJECT_ROOT="$1"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi

WATCHDOG_SCRIPT="$SCRIPT_DIR/talk_watchdog.py"
PYTHON="$PROJECT_ROOT/.venv/bin/python3"

if [ ! -f "$WATCHDOG_SCRIPT" ]; then
    echo "Errore: $WATCHDOG_SCRIPT non trovato"
    exit 1
fi

if [ ! -f "$PYTHON" ]; then
    PYTHON="$(which python3)"
    echo "Nota: .venv non trovato, uso python3 di sistema: $PYTHON"
fi

SERVICE_FILE="/etc/systemd/system/talk-watchdog.service"

echo "Creazione servizio systemd: $SERVICE_FILE"

sudo tee "$SERVICE_FILE" > /dev/null << UNIT
[Unit]
Description=G1 Talk Watchdog (service manager for Talk module)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON $WATCHDOG_SCRIPT --port 8082 --project-root $PROJECT_ROOT
Restart=always
RestartSec=5
Environment=WATCHDOG_TOKEN=${WATCHDOG_TOKEN:-}

[Install]
WantedBy=multi-user.target
UNIT

echo "Ricaricamento systemd..."
sudo systemctl daemon-reload

echo "Abilitazione e avvio servizio..."
sudo systemctl enable talk-watchdog.service
sudo systemctl restart talk-watchdog.service

sleep 2
if systemctl is-active --quiet talk-watchdog.service; then
    echo ""
    echo "talk-watchdog attivo su porta 8082"
    echo "  Stato:    sudo systemctl status talk-watchdog"
    echo "  Log:      sudo journalctl -u talk-watchdog -f"
    echo "  Ferma:    sudo systemctl stop talk-watchdog"
    echo "  Riavvia:  sudo systemctl restart talk-watchdog"
else
    echo "ATTENZIONE: il servizio non sembra attivo. Controlla:"
    echo "  sudo systemctl status talk-watchdog"
    echo "  sudo journalctl -u talk-watchdog --no-pager -n 20"
    exit 1
fi
