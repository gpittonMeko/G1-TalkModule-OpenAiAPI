# Deploy modifiche sull'AI Accelerator
# Uso: .\deploy.ps1

$sshHost = "lab@192.168.10.191"
$root = $PSScriptRoot
$remote = "/home/lab/G1-TalkModule-OpenAiAPI"

Write-Host "Deploy G1 Talk Module su $sshHost" -ForegroundColor Cyan
Write-Host ""

# Copia file Python
Write-Host "  [1] talk_module..." -NoNewline
scp -o ConnectTimeout=10 `
    "$root\talk_module\web_app.py" `
    "$root\talk_module\config.py" `
    "$root\talk_module\audio_robot_effect.py" `
    "$root\talk_module\quick_lookup.py" `
    "$root\talk_module\robot_actions.py" `
    "$root\talk_module\llm\openai_client.py" `
    "${sshHost}:${remote}/talk_module/" 2>$null
scp -o ConnectTimeout=10 "$root\talk_module\tts\openai_tts.py" "${sshHost}:${remote}/talk_module/tts/" 2>$null
scp -o ConnectTimeout=10 `
    "$root\talk_module\stt\fuzzy_correct.py" `
    "$root\talk_module\stt\audio_convert.py" `
    "$root\talk_module\stt\whisper_client.py" `
    "$root\talk_module\stt\groq_client.py" `
    "${sshHost}:${remote}/talk_module/stt/" 2>$null
Write-Host " OK" -ForegroundColor Green

# Copia config (soundboard.json escluso: dati utente sul server, non sovrascrivere)
Write-Host "  [2] config..." -NoNewline
scp -o ConnectTimeout=10 `
    "$root\config\knowledge.json" `
    "$root\config\robot_actions.json" `
    "$root\config\stt_config.json" `
    "$root\config\italian_vocabulary.txt" `
    "$root\config\run_sheet.json" `
    "$root\config\soundboard_script.json" `
    "$root\config\elenco_testi_soundboard.txt" `
    "${sshHost}:${remote}/config/" 2>$null
Write-Host " OK" -ForegroundColor Green

Write-Host "  [2b] soundboard.json (audio, ~5MB)..." -NoNewline
scp -C -o ConnectTimeout=30 -o ServerAliveInterval=30 -o ServerAliveCountMax=120 "$root\config\soundboard.json" "${sshHost}:${remote}/config/"
if ($LASTEXITCODE -eq 0) { Write-Host " OK" -ForegroundColor Green } else { Write-Host " ERRORE scp $LASTEXITCODE" -ForegroundColor Red }

# Installa dipendenze (duckduckgo-search per quick_lookup)
Write-Host "  [3] Dipendenze..." -NoNewline
ssh -o ConnectTimeout=15 $sshHost "cd $remote && .venv/bin/pip install -q ddgs imageio-ffmpeg" 2>$null
Write-Host " OK" -ForegroundColor Green

# Scripts e certificati SSL
Write-Host "  [3b] Scripts + SSL..." -NoNewline
scp -o ConnectTimeout=10 "$root\scripts\restart_server.sh" "$root\scripts\generate_ssl_cert.sh" "$root\scripts\http_redirect.py" "${sshHost}:${remote}/scripts/" 2>$null
ssh -o ConnectTimeout=15 $sshHost "cd $remote && bash scripts/generate_ssl_cert.sh" 2>$null
Write-Host " OK" -ForegroundColor Green

# Riavvia server (timeout 50s: script ~2+8+5*2=20s max)
Write-Host "  [4] Restart server..." -ForegroundColor Gray
$job = Start-Job -ScriptBlock {
    param($h, $r)
    ssh -o ConnectTimeout=15 -o ServerAliveInterval=5 -o ServerAliveCountMax=10 -T $h "cd $r && timeout 45 bash scripts/restart_server.sh"
} -ArgumentList $sshHost, $remote
$null = Wait-Job $job -Timeout 50
$r = Receive-Job $job
Stop-Job $job -ErrorAction SilentlyContinue
Remove-Job $job -Force -ErrorAction SilentlyContinue
Write-Host $r
if ($r -match "OK:200") {
    Write-Host "  [4] HTTP 200 su /api/health" -ForegroundColor Green
} else {
    Write-Host "  [4] Problema riavvio - vedi sopra e tail /tmp/talk.log sul server" -ForegroundColor Red
}

Write-Host ""
Write-Host "  Da telefono (stessa rete):" -ForegroundColor Green
Write-Host "    http://192.168.10.191:8080/client  (redirect a HTTPS)" -ForegroundColor White
Write-Host "    oppure https://192.168.10.191:8081/client" -ForegroundColor White
Write-Host "  Al primo accesso: Avanzate -> Procedi (certificato)" -ForegroundColor Yellow
Write-Host "  Da PC (tunnel): http://localhost:8081/client" -ForegroundColor Cyan
Write-Host "  Ctrl+F5 per evitare cache" -ForegroundColor Gray
Write-Host ""
