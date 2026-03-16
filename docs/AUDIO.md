# Audio - PortAudio e dispositivi

## Installazione PortAudio (Linux/ARM)

```bash
sudo bash scripts/install_audio_jetson.sh
```

Se errore fine riga: `sed -i 's/\r$//' scripts/install_audio_jetson.sh`

## Verifica

```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

## Microfono browser

Richiede localhost (tunnel SSH). Apri http://localhost:8081/client
