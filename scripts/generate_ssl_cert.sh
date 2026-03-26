#!/bin/bash
# Genera certificato SSL self-signed per uso in rete locale (microfono da telefono)
# Uso: bash scripts/generate_ssl_cert.sh [IP_o_hostname]
#   Se omesso: usa TALK_PUBLIC_HOST da ambiente, altrimenti default 192.168.10.191

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 1

CN="${1:-${TALK_PUBLIC_HOST:-192.168.10.191}}"

mkdir -p config/certs
cd config/certs || exit 1

if [ -f key.pem ] && [ -f cert.pem ]; then
  echo "Certificati già presenti in config/certs/"
  exit 0
fi

echo "[generate_ssl_cert] Creazione certificato (CN=${CN})..."
rm -f key.pem cert.pem
# -rand evita attese lunghe su /dev/random (Jetson / embedded)
RAND="-rand /dev/urandom"
if openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes \
  $RAND \
  -subj "/CN=${CN}/O=G1-Talk/C=IT" \
  -addext "subjectAltName=IP:${CN}" 2>/dev/null; then
  :
else
  rm -f key.pem cert.pem
  if ! openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes \
    $RAND \
    -subj "/CN=${CN}/O=G1-Talk/C=IT" 2>/dev/null; then
    echo "Serve openssl. Su Ubuntu: sudo apt install openssl"
    exit 1
  fi
fi

echo "OK: config/certs/key.pem e cert.pem creati (CN=${CN})"
echo "Da telefono sulla stessa rete: https://${CN}:8081/client"
echo "Accetta il certificato al primo accesso."
