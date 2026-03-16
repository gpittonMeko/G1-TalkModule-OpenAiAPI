#!/bin/bash
# Test API text-chat
curl -s -X POST http://127.0.0.1:8081/api/text-chat \
  -H "Content-Type: application/json" \
  -d '{"text":"Ciao"}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('response:', (d.get('response') or '')[:80])
print('duration_ms:', d.get('duration_ms'))
print('ok' if d.get('response') else 'no response')
"
