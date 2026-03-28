# G1 Talk Remote

App Android per gestire e monitorare il servizio G1 Talk sulla Jetson, con soundboard standalone.

## Funzionalita

### Web sul robot dall'APK

- **Client** apre `/client` (stessa UI del browser). **Deep link**: `/client#parla`, `#soundboard`, `#runsheet`, `#knowledge`, `#devices`, `#info`, `#robot`.
- **Parla** (`/client#parla`): ascolto continuo, wake word, STT, LLM, TTS — tutto sul **processo `web_app` sulla Jetson** (WebSocket `/ws` ecc.). Serve **`.env` configurato sul server**, non nell'APK.
- **Robot + Teaching** → `/robot-control`.
- **VR Control** → `/vr-control` (telemetria Quest, API `/api/vr/*` — vedi `vr_teleop_api.py`).
- **Setup** `/setup`, **Listen** `/listen`.

### Dove sta il `.env`

| Cosa | Dove |
|------|------|
| `OPENAI_API_KEY`, STT, wake, LLM per **Parla / pipeline vocale** | File **`.env` nella root del repo sulla Jetson** (caricato da `talk_module.config`) |
| API Key in **Impostazioni APK** | Solo **TTS offline** per soundboard quando la Jetson non c'è; non sostituisce il `.env` del server |

L'APK **non** “installa” il `.env`: è un client. Per far funzionare ascolto continuo e risposta testuale/vocale, la Jetson deve essere raggiungibile e `restart_server.sh` deve aver avviato `web_app` con `.env` valido.

### Dashboard (Tab 1)
- Stato servizio Talk in tempo reale (polling ogni 5 secondi)
- Avvia / Riavvia / Ferma il servizio tramite watchdog
- Visualizzazione log del servizio
- Accesso rapido alla Web UI e Robot Control

### Soundboard: perché non è uguale al robot all'installazione?

L'APK **non** legge `soundboard.json` dal robot finché non fai una **sincronizzazione**: salva una copia in **IndexedDB** sul telefono (offline, BT, ecc.). Alla prima installazione la cache è **vuota**.

1. Imposta l'IP Jetson in **Impostazioni** e assicurati che il badge sia **Connesso** (stessa WiFi, porta 8081, HTTPS se usi SSL).
2. Apri la tab **Soundboard** e tocca **Sincronizza**, oppure lascia che parta la **sync automatica** (una volta per sessione, se cache vuota e Jetson raggiungibile).

### Soundboard (Tab 2)
- Griglia 20 slot (stessa struttura della soundboard web)
- **Modalita connessa**: sincronizza dalla Jetson, riproduce su cassa Jetson o telefono
- **Modalita standalone**: riproduce audio dalla cache locale, genera nuovi TTS via OpenAI
- Selezione uscita audio (speaker/Bluetooth)
- Modifica slot: icona, testo, genera TTS, azioni robot
- Trigger azioni robot (braccio + locomozione) quando connesso

### Impostazioni (Tab 3)
- IP Jetson con preset rapidi
- Porte servizio Talk e Watchdog
- Toggle HTTPS
- API Key OpenAI per TTS standalone
- Voce e modello TTS
- Gestione cache (sincronizza / svuota)

## Requisiti Build

- Node.js (>= 16)
- JDK 17 (`C:\Program Files (x86)\Android\openjdk\jdk-17.0.8.101-hotspot`)
- Android SDK con platform android-32

### Setup SDK utente (consigliato)

```powershell
..\scripts\setup_user_android_sdk.ps1
```

## Build APK

```powershell
cd mobile-app
npm install
.\build_apk.ps1
```

L'APK viene copiato in `dist\G1-Talk-Remote-debug.apk`.

## Installazione Watchdog (Jetson)

Il watchdog e un servizio Python leggero (zero dipendenze esterne) che gira sulla Jetson
sulla porta 8082. Permette all'app di avviare/fermare/riavviare il servizio Talk.

### Deploy sulla Jetson

```bash
# Copia i file sulla Jetson
scp -r mobile-app/jetson-watchdog/ jetson-g1:/home/unitree/G1-TalkModule-OpenAiAPI/mobile-app/jetson-watchdog/

# SSH sulla Jetson
ssh jetson-g1

# Installa come servizio systemd
cd /home/unitree/G1-TalkModule-OpenAiAPI
bash mobile-app/jetson-watchdog/install_watchdog.sh
```

### Sicurezza (opzionale)

Imposta un token di autenticazione:

```bash
export WATCHDOG_TOKEN="un-token-segreto"
# Poi riavvia il servizio o riesegui install_watchdog.sh
```

L'app invia il token come header `Authorization: Bearer <token>`.

### Verifica

```bash
curl http://localhost:8082/health
# {"status": "ok", "service": "talk-watchdog"}

curl http://localhost:8082/talk-status
# {"running": true, "status": "running"}
```

## Architettura

```
Telefono (APK)                    Jetson
+------------------+              +-------------------+
|  Dashboard       |--health----->| web_app.py :8081  |
|  Soundboard      |--soundboard->| (esistente)       |
|  Impostazioni    |              +-------------------+
|                  |              +-------------------+
|                  |--restart---->| watchdog.py :8082 |
|                  |--status----->| (nuovo, separato) |
+------------------+              +-------------------+
        |
        |  (standalone)
        v
  OpenAI API (TTS)
```

## Struttura File

```
mobile-app/
  www/
    index.html          # SPA con 3 tab
    css/app.css         # Stili dark theme
    js/
      app.js            # Controller principale, routing tab
      api.js            # Client HTTP per Jetson (:8081 + :8082)
      openai-tts.js     # TTS diretto via OpenAI (standalone)
      soundboard.js     # Griglia, riproduzione, editing
      services.js       # Dashboard, polling, controllo servizio
      storage.js        # IndexedDB per cache soundboard
      settings.js       # Gestione impostazioni
  android/              # Progetto Android (Capacitor)
  jetson-watchdog/
    talk_watchdog.py    # Servizio HTTP porta 8082
    install_watchdog.sh # Setup systemd
  package.json
  capacitor.config.json
  build_apk.ps1
```

## Note

- L'app NON modifica nessun file esistente del progetto Talk
- Il watchdog usa solo la libreria standard Python (zero dipendenze)
- La soundboard funziona offline con audio pre-sincronizzati dalla Jetson
- Per generare nuovi TTS in standalone serve una API Key OpenAI nelle impostazioni
- L'app ID e `com.g1talk.remote` (diverso da `com.g1talk.app` del launcher esistente)
