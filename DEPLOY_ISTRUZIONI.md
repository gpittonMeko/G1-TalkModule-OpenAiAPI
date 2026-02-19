# Deploy - Istruzioni

Il sito **non si aggiorna da solo**. Devi eseguire il deploy ogni volta che modifichi il codice.

## Metodo 1: Script automatico

Apri PowerShell nella cartella del progetto e esegui:

```powershell
cd c:\Users\user\G1-TalkModule-OpenAiAPI
.\avvia_tutto.ps1
```

Poi apri **http://localhost:8081/client** e ricarica con **Ctrl+F5**.

Se vedi **"Talk Module G1 v2"** in alto, il deploy e andato a buon fine.

## Metodo 2: Passi manuali

### 1. Copia il file
```powershell
scp talk_module\web_app.py lab@192.168.10.191:/home/lab/G1-TalkModule-OpenAiAPI/talk_module/
```

### 2. Riavvia il server (in una finestra PowerShell)
```powershell
ssh lab@192.168.10.191
cd ~/G1-TalkModule-OpenAiAPI
pkill -f talk_module.web_app
nohup .venv/bin/python3 -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check > /tmp/talk.log 2>&1 &
exit
```

### 3. Avvia il tunnel (in un'altra finestra - tienila aperta)
```powershell
ssh -L 8081:localhost:8081 lab@192.168.10.191 -N
```

### 4. Apri il browser
http://localhost:8081/client

## Verifica

- **v2** nel titolo = codice aggiornato
- **Barra livello** sotto il pulsante = indicatore audio attivo
- **Test microfono** = pulsante per provare il mic
