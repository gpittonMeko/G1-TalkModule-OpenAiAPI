#!/bin/bash
echo "Testing /api/led endpoint..."
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"state":"listening"}'
echo ""
echo "---"
curl -sk -X POST https://localhost:8081/api/led \
  -H 'Content-Type: application/json' \
  -d '{"r":0,"g":120,"b":255}'
echo ""
echo "Done."
