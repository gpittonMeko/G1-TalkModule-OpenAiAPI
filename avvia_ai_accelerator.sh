#!/bin/bash
# Avvia G1 Talk Module sull'AI Accelerator
# Esegui su: lab@192.168.10.191 (o sulla macchina AI Accelerator)
# Uso: ./avvia_ai_accelerator.sh

set -e
cd "$(dirname "$0")"

[ -f .env ] || { echo "Crea .env da .env.example e imposta OPENAI_API_KEY"; exit 1; }

if [ -d .venv ]; then
    source .venv/bin/activate
elif [ -d venv ]; then
    source venv/bin/activate
fi

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8081}"

echo "============================================"
echo "G1 Talk Module - AI Accelerator"
echo "============================================"
echo "  http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
echo "  Setup: /"
echo "  Client remoto: /client"
echo "============================================"

python -m talk_module.web_app --host "$HOST" --port "$PORT"
