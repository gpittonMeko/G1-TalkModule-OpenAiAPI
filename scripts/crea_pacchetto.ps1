# Crea pacchetto di installazione G1 Talk Module
# Contiene: tutto il necessario per installare su Linux/Jetson/G1
# Esclude: .git, .venv, cache, file dev, .env (solo .env.example)
# Uso: .\scripts\crea_pacchetto.ps1

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outDir = Join-Path $root "dist"
$zipName = "G1-TalkModule-OpenAiAPI.zip"
$zipPath = Join-Path $outDir $zipName
$tempDir = Join-Path $env:TEMP "G1-TalkModule-pkg"
$pkgRoot = Join-Path $tempDir "G1-TalkModule-OpenAiAPI"

if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
New-Item -ItemType Directory -Path $pkgRoot -Force | Out-Null

robocopy $root $pkgRoot /E /XD .git .venv venv env node_modules __pycache__ temp tmp dist voice-app .cursor /XF *.pyc *.log prova_audio_locale.html deploy.ps1 avvia_tutto.ps1 avvia_installer.ps1 avvia_installer.sh tunnel.ps1 crea_pacchetto.ps1 run.ps1 run.sh main.py /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
# Robocopy: 0=nothing, 1=ok, 2=extra, 8+=error
if ($LASTEXITCODE -ge 8) { Write-Host "Errore robocopy: $LASTEXITCODE"; exit 1 }

# Fix CRLF in shell scripts (Linux)
Get-ChildItem -Path $pkgRoot -Filter "*.sh" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    $c = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
    if ($c -and $c -match "`r`n") {
        $c -replace "`r`n", "`n" | Set-Content $_.FullName -NoNewline
    }
}

# Se .env non esiste nel pacchetto, copia da .env.example
if (-not (Test-Path (Join-Path $pkgRoot ".env"))) {
    $envExample = Join-Path $pkgRoot ".env.example"
    if (Test-Path $envExample) { Copy-Item $envExample (Join-Path $pkgRoot ".env") -Force }
}

# Verifica che i file essenziali ci siano
$required = @("install.sh", "requirements.txt", ".env", "LEGGIMI.txt", "talk_module\web_app.py", "config\knowledge.json")
foreach ($f in $required) {
    if (-not (Test-Path (Join-Path $pkgRoot $f))) {
        Write-Host "ATTENZIONE: manca $f" -ForegroundColor Red
    }
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $pkgRoot -DestinationPath $zipPath -Force
Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue

$size = (Get-Item $zipPath).Length / 1MB
Write-Host ""
Write-Host "Pacchetto creato: $zipPath" -ForegroundColor Green
Write-Host "Dimensione: $([math]::Round($size, 2)) MB" -ForegroundColor Gray
Write-Host ""
Write-Host "Per installare su Linux/Jetson/G1:" -ForegroundColor Yellow
Write-Host "  1. Copia il .zip sulla macchina (scp, usb, ...)"
Write-Host "  2. unzip G1-TalkModule-OpenAiAPI.zip"
Write-Host "  3. cd G1-TalkModule-OpenAiAPI"
Write-Host "  4. bash install.sh"
Write-Host "  5. .env incluso (chiavi gia configurate)"
Write-Host "  6. bash scripts/restart_server.sh"
Write-Host "  7. Apri http://<IP>:8081/client"
Write-Host ""
Write-Host "Guida: LEGGIMI.txt (nel pacchetto)" -ForegroundColor Cyan
