# Un solo comando: avvia server + tunnel. Tieni questa finestra aperta.
# Uso: .\avvia.ps1
# Con deploy: .\avvia.ps1 -deploy  (copia prima i file aggiornati)

param([switch]$deploy)

$sshHost = "lab@192.168.10.191"

Write-Host "G1 Talk Module - Avvio tutto" -ForegroundColor Cyan
Write-Host ""

if ($deploy) {
    Write-Host "[0] Deploy..." -ForegroundColor Yellow
    scp -o ConnectTimeout=10 "$PSScriptRoot\talk_module\web_app.py" "${sshHost}:/home/lab/G1-TalkModule-OpenAiAPI/talk_module/"
    scp -o ConnectTimeout=10 "$PSScriptRoot\talk_module\llm\openai_client.py" "${sshHost}:/home/lab/G1-TalkModule-OpenAiAPI/talk_module/llm/"
    scp -o ConnectTimeout=10 "$PSScriptRoot\talk_module\stt\whisper_client.py" "${sshHost}:/home/lab/G1-TalkModule-OpenAiAPI/talk_module/stt/"
    Write-Host "    Fatto" -ForegroundColor Green
}

# 1. Avvia/riavvia server sull'AI Accelerator
Write-Host "[1/2] Server sull'AI Accelerator..." -ForegroundColor Yellow
$startCmd = 'cd /home/lab/G1-TalkModule-OpenAiAPI && (pkill -f talk_module.web_app 2>/dev/null; true) && sleep 2 && nohup .venv/bin/python3 -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check >> /tmp/talk.log 2>&1 & sleep 3 && curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/'
$code = ssh -o ConnectTimeout=15 $sshHost $startCmd 2>$null
if ($code -eq "200") {
    Write-Host "      Server OK" -ForegroundColor Green
} else {
    Write-Host "      Server avviato (verifica: $code)" -ForegroundColor Gray
}

# 2. Tunnel: questa finestra resta aperta
Write-Host "[2/2] Tunnel SSH - questa finestra deve restare aperta" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Apri: " -NoNewline
Write-Host "http://localhost:8081/client" -ForegroundColor Green
Write-Host ""
Write-Host "  Premi Ctrl+C per chiudere (il server resta attivo)." -ForegroundColor Gray
Write-Host ""

ssh -L 8081:localhost:8081 $sshHost -N
