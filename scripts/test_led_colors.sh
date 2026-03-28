#!/bin/bash
echo "=== LED Color Cycle Test ==="
echo "1. BLU (listening)..."
curl -sk -X POST https://localhost:8081/api/led -H 'Content-Type: application/json' -d '{"r":0,"g":0,"b":255}'
echo ""
sleep 2

echo "2. ROSSO..."
curl -sk -X POST https://localhost:8081/api/led -H 'Content-Type: application/json' -d '{"r":255,"g":0,"b":0}'
echo ""
sleep 2

echo "3. VERDE (speaking)..."
curl -sk -X POST https://localhost:8081/api/led -H 'Content-Type: application/json' -d '{"r":0,"g":255,"b":0}'
echo ""
sleep 2

echo "4. AMBRA (thinking)..."
curl -sk -X POST https://localhost:8081/api/led -H 'Content-Type: application/json' -d '{"r":255,"g":180,"b":0}'
echo ""
sleep 2

echo "5. BIANCO (idle)..."
curl -sk -X POST https://localhost:8081/api/led -H 'Content-Type: application/json' -d '{"r":255,"g":255,"b":255}'
echo ""

echo "=== Done ==="
