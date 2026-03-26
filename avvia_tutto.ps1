# Copia, riavvia server, avvia tunnel - tutto in uno
# Uso: .\avvia_tutto.ps1
# Jetson: $env:G1_SSH_HOST="unitree@192.168.123.164"; $env:G1_REMOTE_PATH="/home/unitree/G1-TalkModule-OpenAiAPI"

$sshHost = if ($env:G1_SSH_HOST) { $env:G1_SSH_HOST } else { "lab@192.168.10.191" }
$remote = if ($env:G1_REMOTE_PATH) { $env:G1_REMOTE_PATH } else { "/home/lab/G1-TalkModule-OpenAiAPI" }

Write-Host "1. Copia web_app.py..." -ForegroundColor Yellow
scp -o ConnectTimeout=10 "$PSScriptRoot\talk_module\web_app.py" "${sshHost}:${remote}/talk_module/web_app.py"
if ($LASTEXITCODE -ne 0) { Write-Host "Copia fallita" -ForegroundColor Red; exit 1 }

Write-Host "2. Riavvio server remoto..." -ForegroundColor Yellow
$restartCmd = "cd '$remote' && pkill -f talk_module.web_app 2>/dev/null; sleep 2; nohup .venv/bin/python3 -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check > /tmp/talk.log 2>&1 & sleep 3; tail -5 /tmp/talk.log"
ssh -o ConnectTimeout=15 $sshHost $restartCmd

Write-Host "3. Avvio tunnel SSH..." -ForegroundColor Yellow
Start-Process -FilePath "ssh" -ArgumentList "-L", "8081:localhost:8081", $sshHost, "-N" -WindowStyle Minimized
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "FATTO. Apri: http://localhost:8081/client" -ForegroundColor Green
Write-Host "Se vedi 'Talk Module G1 v2' il deploy e andato a buon fine." -ForegroundColor Cyan
Write-Host "Ricarica con Ctrl+F5 per evitare cache." -ForegroundColor Cyan