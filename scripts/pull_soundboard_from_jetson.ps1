# Scarica config/soundboard.json dal Jetson sul PC (backup prima di deploy / restore manuale).
# Uso: .\scripts\pull_soundboard_from_jetson.ps1
# Stesse variabili di deploy.ps1: G1_SSH_HOST, G1_REMOTE_PATH, G1_SSH_KEY

$ErrorActionPreference = "Stop"
$sshHost = if ($env:G1_SSH_HOST) { $env:G1_SSH_HOST } else { "jetson-g1" }
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$remote = if ($env:G1_REMOTE_PATH) { $env:G1_REMOTE_PATH } else { "/home/unitree/G1-TalkModule-OpenAiAPI" }
$dest = Join-Path $root "config\soundboard.json.from_jetson"

$sshKeyArgs = @()
if ($env:G1_SSH_KEY) {
    $keyPath = $env:G1_SSH_KEY.Trim().Trim('"')
    if (Test-Path -LiteralPath $keyPath) { $sshKeyArgs = @("-i", $keyPath) }
}
$sshCommon = $sshKeyArgs + @(
    "-o", "ConnectTimeout=15",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new"
)

Write-Host "Scarico soundboard.json da ${sshHost}:${remote}/config/ -> $dest" -ForegroundColor Cyan
scp @sshCommon "${sshHost}:${remote}/config/soundboard.json" $dest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "OK. Rinomina/replace in config\soundboard.json se serve." -ForegroundColor Green
