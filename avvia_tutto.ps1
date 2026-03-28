# Deploy + riavvio + tunnel SSH — tutto in uno
# Uso: .\avvia_tutto.ps1
# Variabili: $env:G1_SSH_HOST, $env:G1_SSH_KEY, $env:G1_SKIP_SOUNDBOARD_JSON, ecc. (vedi deploy.ps1)

$sshHost = if ($env:G1_SSH_HOST) { $env:G1_SSH_HOST } else { "jetson-g1" }

Write-Host "=== Deploy completo ===" -ForegroundColor Cyan
& "$PSScriptRoot\deploy.ps1"
if ($LASTEXITCODE -ne 0) { Write-Host "Deploy fallito" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "=== Avvio tunnel SSH (8081 -> Jetson) ===" -ForegroundColor Yellow
Start-Process -FilePath "ssh" -ArgumentList "-L", "8081:localhost:8081", $sshHost, "-N" -WindowStyle Minimized
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "FATTO. Apri: http://localhost:8081/client" -ForegroundColor Green
Write-Host "Ricarica con Ctrl+F5 per evitare cache." -ForegroundColor Cyan
