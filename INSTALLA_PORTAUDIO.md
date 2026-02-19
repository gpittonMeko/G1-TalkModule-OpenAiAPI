# Installazione PortAudio - G1 Talk Module

## Se hai errore `$'\r': command not found`

I file hanno fine riga Windows. Correggi con:

```bash
cd ~/G1-TalkModule-OpenAiAPI
sed -i 's/\r$//' scripts/install_audio_jetson.sh
```

## Comando da eseguire

```bash
ssh lab@192.168.10.191
cd ~/G1-TalkModule-OpenAiAPI
sed -i 's/\r$//' scripts/install_audio_jetson.sh
sudo bash scripts/install_audio_jetson.sh
```

Ti verrà chiesta la password. Dopo l'installazione, riavvia il modulo:

```bash
cd ~/G1-TalkModule-OpenAiAPI
source .venv/bin/activate
pkill -f talk_module.web_app
python3 -m talk_module.web_app --host 0.0.0.0 --port 8081
```

## Se preferisci manuale

```bash
sudo apt-get update
sudo apt-get install -y portaudio19-dev libsndfile1 ffmpeg
pip install sounddevice
```

## Verifica

```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

Se vedi la lista dei dispositivi, è tutto ok.
