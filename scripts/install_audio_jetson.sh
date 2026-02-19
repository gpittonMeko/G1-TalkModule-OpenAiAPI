#!/bin/bash
# Installa dipendenze audio per G1 Talk Module
# Per Jetson Orin NX, Ubuntu ARM, Raspberry Pi
# Esegui: sudo bash scripts/install_audio_jetson.sh

set -e
echo "=== G1 Talk Module - Installazione audio (PortAudio, FFmpeg) ==="
apt-get update
apt-get install -y portaudio19-dev libsndfile1 libsndfile1-dev python3-dev python3-venv
apt-get install -y ffmpeg || apt-get install -y libav-tools || true

echo ""
echo "=== Verifica librerie ==="
pkg-config --exists portaudio-2.0 && echo "PortAudio: OK" || echo "PortAudio: verifica installazione"
echo ""
echo "=== Completato. Ora esegui nel tuo venv: pip install sounddevice ==="
