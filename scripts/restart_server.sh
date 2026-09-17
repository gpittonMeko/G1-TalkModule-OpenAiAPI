#!/bin/bash
# =============================================================================
# Riavvio stack G1 Talk — UNICO script da usare in produzione (o launch_g1_stack.sh)
#
# Processi avviati:
#   A) python3 -m talk_module.web_app  → :8081  (API, /client, soundboard, robot-action, WebSocket)
#   B) scripts/http_redirect.py        → :8080  SOLO se esistono certificati SSL (redirect HTTP→HTTPS)
#
# Il robot G1 (sport mode, SDK DDS) richiede interfaccia di rete corretta: vedi .env
#   UNITREE_DDS_INTERFACE=usb0   (tipico Jetson collegato al G1 via USB RNDIS, subnet 192.168.123.x)
#   UNITREE_ROBOT_IP=192.168.123.161
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

PYTHON="$PROJECT_ROOT/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  echo "FAIL: interprete Python non trovato: $PYTHON"
  echo "  Questo script va eseguito SUL JETSON (Linux), dentro la cartella del progetto."
  echo "  Da Windows PowerShell usa:"
  echo "    ssh unitree@192.168.123.164 \"cd /home/unitree/G1-TalkModule-OpenAiAPI && bash scripts/restart_server.sh\""
  echo "  Se manca .venv sul Jetson: bash install.sh"
  exit 1
fi

pkill -f talk_module.web_app 2>/dev/null || true
pkill -f http_redirect 2>/dev/null || true
sleep 2
echo "--- $(date) ---" >> /tmp/talk.log
SSL=""
[ -f config/certs/key.pem ] && [ -f config/certs/cert.pem ] && SSL="--ssl"
export PYTHONUNBUFFERED=1
nohup "$PYTHON" -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check $SSL >> /tmp/talk.log 2>&1 &
[ -n "$SSL" ] && nohup "$PYTHON" scripts/http_redirect.py >> /tmp/talk.log 2>&1 &
sleep 8
CURL_OPTS="-s -k --connect-timeout 3 --max-time 8"
_health_code() {
  local proto="$1"
  curl $CURL_OPTS -o /dev/null -w "%{http_code}" "${proto}://127.0.0.1:8081/api/health" 2>/dev/null
}
for i in 1 2 3 4 5; do
  for proto in https http; do
    code=$(_health_code "$proto")
    if [ "$code" = "200" ]; then
      echo "OK:$code"
      for ip in $(hostname -I 2>/dev/null); do
        [ -n "$ip" ] && echo "LAN: https://${ip}:8081/client"
      done
      exit 0
    fi
  done
  sleep 2
done
echo "FAIL - ultimi log:"
tail -30 /tmp/talk.log
echo "--- diagnostica ---"
if pgrep -af talk_module.web_app >/dev/null 2>&1; then
  echo "Processo web_app in esecuzione ma /api/health non risponde 200."
else
  echo "Nessun processo talk_module.web_app attivo."
  echo "Test import Python:"
  "$PYTHON" -c "import talk_module.web_app" 2>&1 | tail -15
fi
exit 1
