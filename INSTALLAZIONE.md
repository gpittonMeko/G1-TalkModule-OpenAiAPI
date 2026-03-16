# Installazione

Vedi **[docs/INSTALLAZIONE.md](docs/INSTALLAZIONE.md)** per la guida completa.

## Quick start

```bash
# Server
bash install.sh

# Oppure manuale
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Inserisci OPENAI_API_KEY
./avvia_ai_accelerator.sh
```

```powershell
# Windows client
.\avvia.ps1
```
