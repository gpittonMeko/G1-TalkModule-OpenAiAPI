#!/bin/bash
echo "=== LED Animation Test ==="

echo "1. ARCOBALENO (thinking) - 6 secondi..."
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"state":"thinking"}'
echo ""
sleep 6

echo "2. BREATHING verde (speaking) - 5 secondi..."
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"state":"speaking"}'
echo ""
sleep 5

echo "3. BLU fisso (listening)..."
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"state":"listening"}'
echo ""
sleep 3

echo "4. BLINK rosso - 4 secondi..."
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"animation":"blink","color":[255,0,0],"speed":1.5}'
echo ""
sleep 4

echo "5. BIANCO fisso (idle)..."
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"state":"idle"}'
echo ""

echo "=== Done ==="
