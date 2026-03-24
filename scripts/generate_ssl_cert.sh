#!/bin/bash
# Genera certificato SSL self-signed per uso in rete locale (microfono da telefono)
# Esegui una volta: bash scripts/generate_ssl_cert.sh

cd "$(dirname "$0")/.." || exit 1
mkdir -p config/certs
cd config/certs || exit 1

if [ -f key.pem ] && [ -f cert.pem ]; then
  echo "Certificati già presenti in config/certs/"
  exit 0
fi

openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes \
  -subj "/CN=192.168.10.191/O=G1-Talk/C=IT" 2>/dev/null || {
  echo "Serve openssl. Su Ubuntu: sudo apt install openssl"
  exit 1
}

echo "OK: config/certs/key.pem e cert.pem creati"
echo "Da telefono sulla stessa rete: https://192.168.10.191:8081/client"
echo "Accetta il certificato al primo accesso."
