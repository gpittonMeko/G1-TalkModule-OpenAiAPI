# Tunnel SSH: accedi a http://localhost:8081/client per usare microfono e cuffie
# Il browser richiede HTTPS o localhost per navigator.mediaDevices
# Uso: .\tunnel.ps1
# Jetson: default jetson-g1 (Host in ~/.ssh/config)

$sshHost = if ($env:G1_SSH_HOST) { $env:G1_SSH_HOST } else { "jetson-g1" }

Write-Host "Avvio tunnel SSH. Apri http://localhost:8081/client nel browser." -ForegroundColor Green
Write-Host "Lascia questa finestra aperta. Premi Ctrl+C per chiudere." -ForegroundColor Yellow
Write-Host ""

ssh -L 8081:localhost:8081 $sshHost -N
