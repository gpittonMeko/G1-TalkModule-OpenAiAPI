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
pkill -f talk_module.web_app 2>/dev/null || true
pkill -f http_redirect 2>/dev/null || true
sleep 2
echo "--- $(date) ---" >> /tmp/talk.log
SSL=""
[ -f config/certs/key.pem ] && [ -f config/certs/cert.pem ] && SSL="--ssl"
nohup .venv/bin/python3 -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check $SSL >> /tmp/talk.log 2>&1 &
[ -n "$SSL" ] && nohup .venv/bin/python3 scripts/http_redirect.py >> /tmp/talk.log 2>&1 &
sleep 8
PROTO="http"
[ -n "$SSL" ] && PROTO="https"
# curl senza --max-time può restare appeso per sempre se 8081 non risponde → deploy/SSH bloccati
CURL_OPTS="-s -k --connect-timeout 3 --max-time 8"
for i in 1 2 3 4 5; do
  code=$(curl $CURL_OPTS -o /dev/null -w "%{http_code}" ${PROTO}://127.0.0.1:8081/api/health 2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "OK:$code"
    exit 0
  fi
  sleep 2
done
echo "FAIL - ultimi log:"
tail -30 /tmp/talk.log
exit 1
