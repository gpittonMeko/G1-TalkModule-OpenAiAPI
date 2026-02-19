# Deploy modifiche sull'AI Accelerator e riavvia il server
# Uso: .\deploy.ps1

$sshHost = "lab@192.168.10.191"
$localFile = Join-Path $PSScriptRoot "talk_module\web_app.py"
$remotePath = "/home/lab/G1-TalkModule-OpenAiAPI/talk_module/web_app.py"

Write-Host "Copia web_app.py su $sshHost ..." -ForegroundColor Yellow
scp -o ConnectTimeout=10 $localFile "${sshHost}:${remotePath}"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Copia fallita." -ForegroundColor Red
    exit 1
}

Write-Host "Riavvio server..." -ForegroundColor Yellow
$cmd = 'cd /home/lab/G1-TalkModule-OpenAiAPI && (pkill -f talk_module.web_app 2>/dev/null || true) && sleep 2 && (nohup .venv/bin/python3 -m talk_module.web_app --host 0.0.0.0 --port 8081 --no-audio-check > /tmp/talk.log 2>&1 &) && sleep 3 && tail -8 /tmp/talk.log'
ssh -o ConnectTimeout=15 $sshHost $cmd
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nModifiche caricate. Apri: http://192.168.10.191:8081/client" -ForegroundColor Green
    Write-Host "Ricarica con Ctrl+F5 per evitare cache." -ForegroundColor Cyan
} else {
    Write-Host "Riavvio fallito." -ForegroundColor Red
}
