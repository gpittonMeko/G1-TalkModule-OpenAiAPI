#!/bin/bash
# Avvia l'installer grafico (configurazione chiave API)
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null || true
python3 -m installer.main
