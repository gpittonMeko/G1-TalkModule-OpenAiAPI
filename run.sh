#!/bin/bash
# G1 Talk Module - Avvio rapido
# Uso: ./run.sh [run|test|list-devices|api]

set -e
cd "$(dirname "$0")"

# Source .env se esiste
[ -f .env ] && set -a && source .env && set +a

# Python virtual env opzionale
if [ -d .venv ]; then
    source .venv/bin/activate
elif [ -d venv ]; then
    source venv/bin/activate
fi

CMD="${1:-run}"
case "$CMD" in
    run)
        python -m talk_module.cli run
        ;;
    once)
        python -m talk_module.cli run --once
        ;;
    test-stt)
        python -m talk_module.cli test stt
        ;;
    test-tts)
        python -m talk_module.cli test tts --text "Test riproduzione"
        ;;
    list-devices)
        python -m talk_module.cli list-devices
        ;;
    api)
        python -m talk_module.api_server --host 0.0.0.0 --port 8081
        ;;
    *)
        echo "Uso: ./run.sh {run|once|test-stt|test-tts|list-devices|api}"
        exit 1
        ;;
esac
