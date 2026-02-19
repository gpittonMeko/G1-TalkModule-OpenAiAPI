# G1 Talk Module - OpenAI API

Add-on vocale: **microfono → STT (Whisper) → LLM (GPT) → TTS → speaker**.  
Gira **solo sull'AI Accelerator** (192.168.10.191). Il robot ha un altro IP.

---

## Architettura

- **AI Accelerator**: macchina dove installi e fai girare il modulo (192.168.10.191)
- **Setup wizard**: alla prima apertura, selezioni microfono e altoparlante nella rete
- **Dispositivi locali**: microfoni/altoparlanti collegati alla macchina
- **Dispositivi web nella rete**: telefono o altro device che apre la pagina `/client` e diventa mic+speaker

---

## Installazione sull'AI Accelerator

```bash
ssh lab@192.168.10.191
cd ~/G1-TalkModule-OpenAiAPI   # o path dove cloni

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Modifica .env e inserisci OPENAI_API_KEY
```

### Dipendenze sistema (su ARM/Linux)

```bash
sudo apt install -y portaudio19-dev libsndfile1 ffmpeg
```

---

## Avvio

```bash
./avvia_ai_accelerator.sh
# oppure
python -m talk_module.web_app --host 0.0.0.0 --port 8081
```

Poi apri nel browser: **http://192.168.10.191:8081**

---

## Flusso utente

1. **Setup** (`/`): Seleziona microfono e altoparlante
   - Se scegli dispositivi **locali**: microfoni/speaker collegati alla macchina
   - Se scegli **Dispositivo web nella rete**: apri `/client` sul telefono

2. **Salva e avvia**: la scelta viene salvata in `config/audio_devices.json`

3. **Uso**:
   - **Locale** (`/local`): clic su "Parla", registra 8 sec, elabora, riproduce localmente
   - **Web** (`/client`): apri sul telefono, tieni premuto per registrare, risposta via speaker del telefono

---

## Configurazione (.env)

| Variabile | Descrizione | Default |
|-----------|-------------|---------|
| `OPENAI_API_KEY` | **Obbligatorio** | - |
| `TTS_LANGUAGE` | Lingua (it, en...) | it |
| `LLM_MODEL` | gpt-4o-mini, gpt-4o | gpt-4o-mini |
| `TTS_VOICE` | alloy, echo, shimmer... | shimmer |

---

## Struttura

```
G1-TalkModule-OpenAiAPI/
├── talk_module/
│   ├── web_app.py      # App principale (setup + controllo)
│   ├── audio/          # Recorder, Player
│   ├── stt/, llm/, tts/
│   └── config.py
├── config/
│   └── audio_devices.json   # Scelte mic/speaker (creato al primo save)
├── avvia_ai_accelerator.sh
├── requirements.txt
└── .env.example
```

---

## Note sicurezza

- Non committare mai `.env` o chiavi API
