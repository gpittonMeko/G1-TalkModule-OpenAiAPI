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
    "$root\talk_module\quick_lookup.py" `
    "$root\talk_module\robot_actions.py" `
    "$root\talk_module\llm\openai_client.py" `
    "${sshHost}:${remote}/talk_module/" 2>$null
scp -o ConnectTimeout=10 "$root\talk_module\stt\fuzzy_correct.py" "${sshHost}:${remote}/talk_module/stt/" 2>$null
Write-Host " OK" -ForegroundColor Green

# Copia config
Write-Host "  [2] config..." -NoNewline
scp -o ConnectTimeout=10 `
    "$root\config\knowledge.json" `
    "$root\config\robot_actions.json" `
    "$root\config\stt_config.json" `
    "$root\config\soundboard.json" `
    "$root\config\italian_vocabulary.txt" `
    "${sshHost}:${remote}/config/" 2>$null
Write-Host " OK" -ForegroundColor Green

# Installa dipendenze (duckduckgo-search per quick_lookup)
Write-Host "  [3] Dipendenze..." -NoNewline
ssh -o ConnectTimeout=15 $sshHost "cd $remote && .venv/bin/pip install -q ddgs" 2>$null
Write-Host " OK" -ForegroundColor Green

# Riavvia server
Write-Host "  [4] Restart server..." -NoNewline
$r = ssh -o ConnectTimeout=15 $sshHost "cd $remote && bash scripts/restart_server.sh" 2>&1
if ($r -match "OK:200") {
    Write-Host " OK" -ForegroundColor Green
} else {
    Write-Host " (check log)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Apri: http://localhost:8081/client (con tunnel attivo)" -ForegroundColor Green
Write-Host "  Ctrl+F5 per evitare cache" -ForegroundColor Gray
Write-Host ""
