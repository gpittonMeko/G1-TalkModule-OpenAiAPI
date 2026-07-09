# G1 Talk Module – Guida rapida

Assistente vocale per robot **Unitree G1**: parli → STT → GPT → TTS → risposta.

---

## Come ti connetti

Hai **due modi** per usare il Talk Module:

### Opzione A: Bridge (PC → rete AI Accelerator)

```
┌─────────────┐    tunnel SSH     ┌──────────────────┐
│  Tuo PC     │ ◄───────────────► │  AI Accelerator  │
│  Browser    │  localhost:8081   │  192.168.10.191  │
│  Mic+Cuffie │                   │  (server)        │
└─────────────┘                   └──────────────────┘
```

- Il server gira sull’AI Accelerator (o su un PC nella stessa rete).
- Dal tuo PC apri un tunnel SSH e usi mic e cuffie del browser.
- **Comando:** `ssh -L 8081:localhost:8081 lab@192.168.10.191 -N`
- Poi apri: **http://localhost:8081/client**

---

### Opzione B: Diretto al WiFi del G1

```
┌─────────────┐                   ┌──────────────────┐
│  Telefono   │     WiFi G1       │  G1 (o PC/Jetson) │
│  o Tablet   │ ◄───────────────► │  stesso WiFi      │
│  Browser    │                   │  server:8081      │
└─────────────┘                   └──────────────────┘
```

- Ti colleghi al WiFi del robot (o a una rete dove gira il server).
- Apri il browser e vai a: **http://&lt;IP-SERVER&gt;:8081/client**
- Esempio: `http://192.168.123.161:8081/client` se il G1/PC ha quell’IP.

---

## Pacchetto completo (G1 + audio + APK)

Su Windows, per generare **un solo zip** con server Linux, file audio soundboard e cartella APK:

```powershell
.\scripts\prepara_pacchetto_completo_g1.ps1
```

Si creano `dist/G1_Pacchetto_Installazione_Completa.zip` e la cartella omonima:
- `01_Server_UnitreeG1/` — `G1-TalkModule-OpenAiAPI.zip` (installa sul G1 con `install.sh`)
- `02_Soundboard_Audio/` — `.wav` + `testi_soundboard.txt`
- `03_APK_Android/` — APK se la build Gradle riesce; altrimenti `LEGGIMI_APK.txt` e istruzioni

Solo server (senza assemblaggio): `.\scripts\crea_pacchetto.ps1` → `dist/G1-TalkModule-OpenAiAPI.zip`.

---

## Installazione veloce (pacchetto server)

### 1. Ottieni il pacchetto

```powershell
# Da Windows (nella cartella del progetto)
.\scripts\crea_pacchetto.ps1
```

Si crea `dist/G1-TalkModule-OpenAiAPI.zip`.

### 2. Copia sulla macchina dove gira il server

- USB, SCP, ecc.  
- Esempio: `scp dist/G1-TalkModule-OpenAiAPI.zip lab@192.168.10.191:~/`

### 3. Installa (Linux)

```bash
unzip G1-TalkModule-OpenAiAPI.zip
cd G1-TalkModule-OpenAiAPI
bash install.sh
```

**Jetson G1 (computer di bordo)** — dopo `install.sh` servono anche **Unitree SDK** (braccia/loco) e **OpenCV** (dashboard camera):

```bash
bash scripts/install_jetson_completo.sh
```

Vedi **[docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md](docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md)**.

### 4. Configura le chiavi API

Modifica `.env` e inserisci almeno:

```
OPENAI_API_KEY=sk-tua-chiave-qui
```

Opzionale (STT più veloce):

```
GROQ_API_KEY=tua-chiave-groq
```

### 5. Avvia

```bash
bash scripts/restart_server.sh
```

Poi apri **http://&lt;IP&gt;:8081/client** (o localhost:8081 con tunnel).

Sul **Jetson del G1** usa **`bash scripts/install_jetson_completo.sh`** (base + SDK + OpenCV/YOLO).  
`install.sh` da solo non installa `unitree_sdk2py` né `opencv` — guida **[docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md](docs/INSTALLAZIONE_G1_JETSON_COMPLETA.md)**.

---

## Struttura dopo l’installazione

```
G1-TalkModule-OpenAiAPI/
├── .env              ← Chiavi API (da modificare)
├── config/           ← knowledge, robot_actions, stt
├── talk_module/      ← Codice
├── install.sh        ← Installazione
└── scripts/
    └── restart_server.sh   ← Avvia/riavvia
```

---

## Pagine web

| Pagina    | Uso                                      |
|-----------|------------------------------------------|
| `/`       | Setup mic/speaker                        |
| `/client` | **Principale** – tieni premuto e parla   |
| `/local`  | Push-to-talk con mic locale sul server   |
| `/listen` | Ascolto continuo: "Hey G1" + domanda     |

---

## Riepilogo comandi

| Azione              | Comando                                  |
|---------------------|------------------------------------------|
| Crea pacchetto      | `.\scripts\crea_pacchetto.ps1`           |
| Installa (Linux)    | `bash install.sh` o `bash INSTALLA`       |
| Installa Jetson G1  | `bash scripts/install_jetson_completo.sh` |
| Avvia server        | `bash scripts/restart_server.sh`         |
| Da Windows (tunnel) | `.\avvia.ps1`                            |

---

## Configurazione .env

Il pacchetto include `.env.example`. Dopo l'installazione:

1. Copia: `cp .env.example .env`
2. Modifica `.env` e inserisci almeno: `OPENAI_API_KEY=sk-tua-chiave`
3. Opzionale: `GROQ_API_KEY` per STT più veloce (gratuito su groq.com)
