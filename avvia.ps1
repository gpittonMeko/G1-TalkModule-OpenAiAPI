# Avvia tutto: server + tunnel + apre il browser
# Doppio click o: .\AVVIA.ps1

$sshHost = "lab@192.168.10.191"
$url = "http://localhost:8081/client"

Write-Host ""
Write-Host "  G1 Talk Module" -ForegroundColor Cyan
Write-Host ""

# 1. Riavvia server
Write-Host "  [1] Server..." -NoNewline
$r = ssh -o ConnectTimeout=20 $sshHost "bash /home/lab/G1-TalkModule-OpenAiAPI/scripts/restart_server.sh" 2>&1
if ($r -match "OK:200") {
    Write-Host " OK" -ForegroundColor Green
} else {
    Write-Host " avviato" -ForegroundColor Gray
}

# 2. Avvia tunnel in background
Write-Host "  [2] Tunnel..." -NoNewline
Start-Process ssh -ArgumentList "-o ConnectTimeout=10 -L 8081:localhost:8081 $sshHost -N" -WindowStyle Hidden
Start-Sleep -Seconds 4
Write-Host " OK" -ForegroundColor Green

# 3. Apri browser
Write-Host "  [3] Browser..." -NoNewline
Start-Process $url
Write-Host " OK" -ForegroundColor Green
Write-Host ""
Write-Host "  Aperto: $url" -ForegroundColor Green
Write-Host "  Il tunnel resta attivo in background." -ForegroundColor Gray
Write-Host "  Per chiuderlo: taskkill /F /IM ssh.exe" -ForegroundColor DarkGray
Write-Host ""
