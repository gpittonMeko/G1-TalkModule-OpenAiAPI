# Avvio locale su Windows (robot spento) - test modulo vocale + Gemini
# Uso: powershell -ExecutionPolicy Bypass -File .\avvia_locale.ps1
# Prima: copy .env.example .env e imposta OPENAI_API_KEY + GEMINI_API_KEY + LLM_PROVIDER=gemini

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "ERRORE: manca .env" -ForegroundColor Red
    Write-Host "  copy .env.example .env" -ForegroundColor Yellow
    Write-Host "  Poi imposta OPENAI_API_KEY, GEMINI_API_KEY, LLM_PROVIDER=gemini" -ForegroundColor Yellow
    exit 1
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creo .venv e installo dipendenze..." -ForegroundColor Cyan
    python -m venv .venv
    & $venvPy -m pip install -q -U pip
    & $venvPy -m pip install -q -r requirements.txt
}

Write-Host ""
Write-Host "  G1 Talk - locale (Gemini LLM)" -ForegroundColor Cyan
Write-Host "  Apri: http://127.0.0.1:8081/client" -ForegroundColor Green
Write-Host "  Tab Parla, Consenti microfono, scrivi o Ascolto continuo" -ForegroundColor Gray
Write-Host ""
Write-Host "  Test LLM: .\.venv\Scripts\python.exe -m talk_module.cli test llm" -ForegroundColor Gray
Write-Host ""

& $venvPy -m talk_module.web_app --host 127.0.0.1 --port 8081 --no-audio-check
