#!/usr/bin/env bash
# Scarica yolov8n.onnx (~12.8 MB) per camera dashboard (OpenCV DNN, no PyTorch).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ROOT}/config/models/yolov8n.onnx"
mkdir -p "$(dirname "$DEST")"
if [[ -f "$DEST" ]] && [[ "$(wc -c <"$DEST")" -ge 5000000 ]]; then
  echo "OK: $DEST ($(wc -c <"$DEST") byte)"
  exit 0
fi
rm -f "$DEST"
URL="https://huggingface.co/Kalray/yolov8/resolve/main/yolov8n.onnx"
echo "Download $URL -> $DEST"
curl -fL --retry 3 --retry-delay 2 -o "$DEST" "$URL"
SZ=$(wc -c <"$DEST")
if [[ "$SZ" -lt 5000000 ]]; then
  echo "ERRORE: file troppo piccolo ($SZ byte). Cancella e riprova." >&2
  rm -f "$DEST"
  exit 1
fi
echo "OK: $DEST ($SZ byte)"
