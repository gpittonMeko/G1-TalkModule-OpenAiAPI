# Copia il progetto sul Jetson in /home/unitree/G1-TalkModule-OpenAiAPI (cartella ordinata).
# Esclude .venv, .git, cache e build pesanti — sul Jetson poi: bash install.sh (vedi sotto).
#
# Uso (PowerShell, dalla cartella del repo):
#   .\scripts\copia_su_jetson.ps1
#
# Parametri:
#   -SshTarget jetson-g1
#   -SshTarget unitree@192.168.123.164
#   -RemotePath /home/unitree/G1-TalkModule-OpenAiAPI

param(
    [string]$SshTarget = "jetson-g1",
    [string]$RemotePath = "/home/unitree/G1-TalkModule-OpenAiAPI"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $ProjectRoot "install.sh"))) {
    Write-Host "Non trovo install.sh. Esegui lo script dalla cartella G1-TalkModule-OpenAiAPI." -ForegroundColor Red
    exit 1
}

$Staging = Join-Path $env:TEMP "G1TalkModule_staging_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

Write-Host "Origine:  $ProjectRoot" -ForegroundColor Cyan
Write-Host "Staging:  $Staging" -ForegroundColor Gray
Write-Host "Destino:  ${SshTarget}:$RemotePath" -ForegroundColor Cyan
Write-Host ""
Write-Host "Copia in staging (esclusi .venv, .git, node_modules, dist, .cursor)..." -ForegroundColor Yellow

robocopy $ProjectRoot $Staging /E /NFL /NDL /NJH /NJS /XD .venv .git __pycache__ node_modules dist .cursor .mypy_cache .pytest_cache | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "robocopy errore (codice $LASTEXITCODE)" -ForegroundColor Red
    exit 1
}

Write-Host "Creazione cartella remota e invio (scp)..." -ForegroundColor Yellow
ssh $SshTarget "mkdir -p '$RemotePath'"

Push-Location $Staging
try {
    Get-ChildItem -Force | ForEach-Object {
        $name = $_.Name
        Write-Host "  -> $name" -ForegroundColor DarkGray
        scp -r $name "${SshTarget}:${RemotePath}/"
        if ($LASTEXITCODE -ne 0) {
            throw "scp fallito per $name (codice $LASTEXITCODE)"
        }
    }
}
finally {
    Pop-Location
}

Remove-Item -Recurse -Force $Staging -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Fatto. Sul Jetson:" -ForegroundColor Green
Write-Host "  ssh $SshTarget" -ForegroundColor White
Write-Host "  cd $RemotePath" -ForegroundColor White
Write-Host "  cp .env.example .env && nano .env   # OPENAI_API_KEY, TALK_PUBLIC_HOST=<IP_Jetson>" -ForegroundColor White
Write-Host "  PYTHON=python3.10 bash install.sh --no-audio   # adatta PYTHON se serve" -ForegroundColor White
Write-Host "  TALK_PUBLIC_HOST=<IP> bash scripts/generate_ssl_cert.sh" -ForegroundColor White
Write-Host "  bash scripts/restart_server.sh" -ForegroundColor White
Write-Host ""
