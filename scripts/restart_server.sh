#!/bin/bash
# Riavvia G1 Talk Module sull'AI Accelerator
# HTTPS su 8081 (microfono da telefono). HTTP su 8080 reindirizza a HTTPS.

cd /home/lab/G1-TalkModule-OpenAiAPI || exit 1
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
