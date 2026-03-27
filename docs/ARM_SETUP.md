# Setup ARM - Jetson Orin NX, Raspberry Pi, Linux ARM

## Jetson G1: Unitree SDK (robot)

Per controllo braccia e locomozione via DDS: **[JETSON_UNITREE_SDK.md](JETSON_UNITREE_SDK.md)** (`scripts/install_unitree_sdk_jetson.sh`).

## Obbligatorio: PortAudio per microfoni e altoparlanti locali

Su **Jetson Orin NX** (robot G1) e ogni macchina con audio locale:

```bash
cd ~/G1-TalkModule-OpenAiAPI
sudo bash scripts/install_audio_jetson.sh
```

Questo installa: `portaudio19-dev`, `libsndfile1`, `ffmpeg`.

## Senza PortAudio (solo dispositivi di rete)

Se non puoi installare (es. test senza sudo), avvia con:
```bash
python -m talk_module.web_app --no-audio-check
```
Funzioneranno solo i dispositivi che aprono `/client` (telefono, tablet).

## Dipendenze sistema

```bash
# Ubuntu/Debian ARM (Raspberry Pi, Jetson)
sudo apt update
sudo apt install -y \
    portaudio19-dev \
    libsndfile1 \
    ffmpeg \
    python3-dev \
    python3-venv
```

## PortAudio

Il modulo `sounddevice` usa PortAudio. Su ARM a volte serve:

```bash
# Verifica
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

Se manca: `sudo apt install portaudio19-dev` e `pip install sounddevice`.

## Riproduzione audio

L'ordine di fallback per la riproduzione è:

1. **ffplay** (ffmpeg) - preferito su Linux
2. **mpg123** - per MP3
3. **aplay** - ALSA (Raspberry Pi senza PulseAudio)
4. **paplay** - PulseAudio
5. **sounddevice** - Python puro

Su Raspberry Pi senza desktop: `aplay` o `mpg123` sono di solito disponibili. Installa ffmpeg se possibile:

```bash
sudo apt install ffmpeg
```

## Test rapido

```bash
# 1. Dispositivi
python -m talk_module.cli list-devices

# 2. TTS (senza microfono)
python -m talk_module.cli test tts --text "Ciao da ARM"

# 3. STT (con microfono)
python -m talk_module.cli test stt

# 4. Conversazione completa
python -m talk_module.cli run --once -d 5
```

## Problemi comuni

### "No module named 'sounddevice'"
```bash
sudo apt install portaudio19-dev
pip install sounddevice
```

### "No default input device"
- Collega un microfono USB o verifica che l'audio built-in sia abilitato
- Usa `list-devices` per trovare l'ID e imposta `MICROPHONE_DEVICE_ID` nel `.env`

### Riproduzione non funziona
- Prova: `ffplay -nodisp -autoexit test.mp3`
- Se manca ffplay: `sudo apt install ffmpeg`
- Su sistemi minimali: `sudo apt install mpg123`
