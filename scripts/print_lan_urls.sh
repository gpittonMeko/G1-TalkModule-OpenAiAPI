#!/bin/bash
# Stampa gli URL per aprire l'app dal telefono/PC sulla stessa rete (router o WiFi G1).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

IPS=$(hostname -I 2>/dev/null | tr -s ' ')
PUBLIC="${TALK_PUBLIC_HOST:-}"
if [ -f .env ]; then
  val=$(grep -E '^TALK_PUBLIC_HOST=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
  [ -n "$val" ] && PUBLIC="$val"
fi

echo "=============================================="
echo " G1 Talk — accesso da rete locale (router)"
echo "=============================================="
echo ""
echo "Sul telefono/PC (stessa rete del robot):"
for ip in $IPS $PUBLIC; do
  [ -z "$ip" ] && continue
  echo "  https://${ip}:8081/client"
  echo "  https://${ip}:8081/client#occhi   (camera)"
  echo "  http://${ip}:8080/client          (redirect → HTTPS)"
  echo ""
done
echo "Verifica server:"
echo "  curl -sk https://127.0.0.1:8081/api/health"
echo ""
echo "Se il PC non raggiunge 192.168.123.x dal router TP-Link:"
echo "  Windows (admin): .\\scripts\\add_g1_subnet_windows.ps1"
echo "  oppure collega il PC al WiFi del G1 (subnet 192.168.123.x)"
echo "=============================================="
