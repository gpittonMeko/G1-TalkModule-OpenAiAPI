# G1 Talk Module - Avvio rapido (PowerShell)
# Uso: .\run.ps1 [run|test|list-devices|api]

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$cmd = if ($args.Count -gt 0) { $args[0] } else { "run" }
switch ($cmd) {
    "run"       { python -m talk_module.cli run }
    "once"      { python -m talk_module.cli run --once }
    "test-stt"  { python -m talk_module.cli test stt }
    "test-tts"  { python -m talk_module.cli test tts --text "Test riproduzione" }
    "list-devices" { python -m talk_module.cli list-devices }
    "api"       { python -m talk_module.api_server --host 0.0.0.0 --port 8081 }
    default     { Write-Host "Uso: .\run.ps1 {run|once|test-stt|test-tts|list-devices|api}"; exit 1 }
}
