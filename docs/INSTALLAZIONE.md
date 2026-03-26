# G1 Talk Module – Guida installazione

## Requisiti

- **Sistema**: Linux (Ubuntu 20.04+, Debian, Jetson) o Windows (solo client)
- **Python**: 3.10+
- **Rete**: connessione internet per API OpenAI
- **API Key**: OpenAI (obbligatoria)

---

## Python su Jetson / Ubuntu 20.04

Il sistema può avere solo **Python 3.8** come `python3`. Questo progetto richiede **3.10+**.

Verifica:

```bash
python3 --version
```

Se è minore di 3.10, installa 3.10 (esempio con PPA deadsnakes su Ubuntu x86_64; su Jetson ARM usa i pacchetti disponibili per la tua immagine, oppure `python3.10` da sorgente / immagine aggiornata):

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3.10-dev
cd ~/G1-TalkModule-OpenAiAPI
PYTHON=python3.10 bash install.sh
```

Su alcune immagini Jetson il PPA non è supportato: in quel caso usa un’immagine Ubuntu 22.04+ per Jetson, o compila Python 3.10+, come indicato nella documentazione NVIDIA/L4T.

---

## Installazione su Jetson (es. `unitree@192.168.123.164`)

1. Copia il progetto in `~/G1-TalkModule-OpenAiAPI` (git clone, scp, zip).
2. In `.env` imposta almeno `OPENAI_API_KEY` e, per redirect HTTP→HTTPS e certificato coerenti con la LAN:

   ```env
   TALK_PUBLIC_HOST=192.168.123.164
   ```

3. Installazione (tutto il progetto e il venv restano in `~/G1-TalkModule-OpenAiAPI`; Python 3.10 di sistema va installato con `apt`):

   ```bash
   cd ~/G1-TalkModule-OpenAiAPI
   bash scripts/install_jetson_tutto_in_cartella.sh
   ```

   In alternativa manuale: `PYTHON=python3.10 bash install.sh --no-audio`

   (`--no-audio` se usi solo browser/telefono sulla rete e non ti serve PortAudio sul Jetson.)

4. Certificati (CN = IP che usi dal telefono):

   ```bash
   TALK_PUBLIC_HOST=192.168.123.164 bash scripts/generate_ssl_cert.sh
   ```

   oppure `bash scripts/generate_ssl_cert.sh 192.168.123.164`

5. Avvio:

   ```bash
   bash scripts/restart_server.sh
   ```

6. Test: `curl -k https://127.0.0.1:8081/api/health` — dal PC: `https://192.168.123.164:8081/client`

Da Windows, senza modificare gli script, puoi impostare:

```powershell
$env:G1_SSH_HOST="unitree@192.168.123.164"
$env:G1_REMOTE_PATH="/home/unitree/G1-TalkModule-OpenAiAPI"
$env:G1_PUBLIC_IP="192.168.123.164"
.\deploy.ps1
```

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
