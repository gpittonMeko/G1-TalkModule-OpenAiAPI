#!/bin/bash
# Riavvia G1 Talk Module sull'AI Accelerator

cd /home/lab/G1-TalkModule-OpenAiAPI || exit 1
pkill -f talk_module.web_app 2>/dev/null || true
sleep 2
echo "--- $(date) ---" >> /tmp/talk.log
nohup .venv/bin/python3 -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check >> /tmp/talk.log 2>&1 &
sleep 8
for i in 1 2 3 4 5; do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/api/health 2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "OK:$code"
    exit 0
  fi
  sleep 2
done
echo "FAIL - ultimi log:"
tail -30 /tmp/talk.log
exit 1
