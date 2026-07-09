# G1 Talk Module

Assistente vocale per robot **Unitree G1**: parli → STT → GPT → TTS → risposta.

- **Wake word**: "Hey G1" (o "Hey Markone")
- **Azioni robot**: "Dare la mano", "Saluta" → Unitree SDK
- **Ricerca veloce**: ora, meteo, domande base (senza LLM)
- **STT**: Whisper, Groq o Deepgram

---

## Guida completa

**Leggi [GUIDA.md](GUIDA.md)** per:

- Come connetterti (bridge o WiFi diretto al G1)
- Installazione veloce con pacchetto
- Configurazione .env e chiavi API

---

## Configurazione

### .env (obbligatorio)

| Variabile | Descrizione |
|-----------|-------------|
| `OPENAI_API_KEY` | **Obbligatorio** – chiave OpenAI |
| `TTS_LANGUAGE` | it, en, ... (default: it) |
| `LLM_MODEL` | gpt-4o-mini, gpt-4o (default: gpt-4o-mini) |
| `TTS_VOICE` | shimmer, nova, alloy... (default: shimmer) |

### STT (Speech-to-Text)

| Variabile | Descrizione |
|-----------|-------------|
| `STT_PROVIDER` | whisper, groq, deepgram |
| `GROQ_API_KEY` | Per Groq (veloce, gratuito) |
| `DEEPGRAM_API_KEY` | Per Deepgram |

### Robot G1 (azioni vocali)

| Variabile | Descrizione |
|-----------|-------------|
| `UNITREE_ROBOT_IP` | IP del robot per "dare la mano", "saluta" |

**Jetson (nuovo G1)**: `bash install.sh` **non basta** — serve anche SDK movimenti e OpenCV visione:

```bash
bash scripts/install_jetson_completo.sh
```

Guida: [docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md](docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md) · SDK: [docs/JETSON_UNITREE_SDK.md](docs/JETSON_UNITREE_SDK.md)

---

## Struttura progetto

```
G1-TalkModule-OpenAiAPI/
├── talk_module/
│   ├── web_app.py          # App principale
│   ├── robot_actions.py    # Routing azioni G1 (dare la mano, saluta)
│   ├── config.py
│   ├── stt/                # Whisper, Groq, Deepgram + fuzzy correct
│   ├── llm/                # OpenAI Chat
│   ├── tts/                # OpenAI TTS
│   └── audio/              # Recorder, Player
├── config/
│   ├── knowledge.json      # Risposte veloci (pattern → risposta)
│   ├── robot_actions.json  # Comandi vocali → azioni SDK
│   ├── stt_config.json     # Fuzzy STT, extra_phrases
│   └── italian_vocabulary.txt
├── scripts/
│   ├── restart_server.sh
│   └── robot_action.sh     # (opzionale) script azioni custom
├── docs/                   # Documentazione (es. JETSON_UNITREE_SDK.md)
├── avvia.ps1               # Avvio da Windows
├── avvia_ai_accelerator.sh  # Avvio server
├── requirements.txt
├── requirements-camera.txt   # OpenCV (visione dashboard)
├── requirements-jetson.txt     # base + camera (pip)
└── .env.example
```

---

## Modalità d'uso

| Pagina | Descrizione |
|--------|-------------|
| `/` | Setup: seleziona microfono e altoparlante |
| `/client` | **Principale** – mic/cuffie del browser, tieni premuto per parlare |
| `/local` | Push-to-talk locale (mic/speaker sull'AI Accelerator) |
| `/listen` | Ascolto continuo: "Hey G1" + domanda |

---

## Deploy (aggiornare il server)

Dopo modifiche al codice:

```powershell
# Copia file modificati
scp talk_module\web_app.py talk_module\config.py config\*.json lab@192.168.10.191:~/G1-TalkModule-OpenAiAPI/

# Riavvia
ssh lab@192.168.10.191 "cd ~/G1-TalkModule-OpenAiAPI && bash scripts/restart_server.sh"
```

Oppure usa `.\deploy.ps1` se configurato.

---

## Documentazione

- **[GUIDA.md](GUIDA.md)** - Guida rapida: connessione, installazione, pacchetto
- **[docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md](docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md)** - Nuovo G1: SDK + OpenCV + checklist
- [docs/INSTALLAZIONE.md](docs/INSTALLAZIONE.md) - Guida installazione dettagliata
- [docs/JETSON_UNITREE_SDK.md](docs/JETSON_UNITREE_SDK.md) - Unitree SDK2 / Cyclone DDS
- [docs/STT.md](docs/STT.md) - Provider STT e correzioni fuzzy
- [docs/ROBOT_ACTIONS.md](docs/ROBOT_ACTIONS.md) - Azioni vocali G1
- [docs/AUDIO.md](docs/AUDIO.md) - PortAudio e dispositivi

---

## Note sicurezza

- Non committare `.env` o chiavi API
- Il tunnel SSH è necessario per usare il microfono da browser (localhost)
