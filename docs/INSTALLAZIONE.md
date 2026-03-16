# G1 Talk Module – Guida installazione

## Requisiti

- **Sistema**: Linux (Ubuntu 20.04+, Debian, Jetson) o Windows (solo client)
- **Python**: 3.10+
- **Rete**: connessione internet per API OpenAI
- **API Key**: OpenAI (obbligatoria)

---

## Installazione sul server (AI Accelerator)

### 1. Clona o copia il progetto

```bash
cd ~
unzip G1-TalkModule-OpenAiAPI.zip
cd G1-TalkModule-OpenAiAPI
```

### 2. Ambiente virtuale e dipendenze

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Dipendenze sistema (Linux/ARM)

```bash
sudo apt install -y portaudio19-dev libsndfile1 ffmpeg
```

Su Jetson: `sudo bash scripts/install_audio_jetson.sh`

### 4. Configurazione

```bash
cp .env.example .env
nano .env
```

Inserisci: `OPENAI_API_KEY=sk-la-tua-chiave`

### 5. Avvio

```bash
./avvia_ai_accelerator.sh
```

Apri: **http://192.168.10.191:8081**

---

## Client da Windows

Tunnel SSH + browser:

```powershell
ssh -L 8081:localhost:8081 lab@192.168.10.191 -N
```

Poi: **http://localhost:8081/client**

Oppure: `.\avvia.ps1` (tutto in uno)

---

## Risoluzione problemi

- **Connection refused**: verifica server con `curl http://192.168.10.191:8081/api/health`
- **Microfono**: usa localhost (tunnel), non IP diretto
- **Nessun testo**: parla 1-2 sec, prova `STT_PROVIDER=groq`
