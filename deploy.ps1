# Deploy modifiche sul server Linux (AI Accelerator, Jetson unitree, ecc.)
# Uso: .\deploy.ps1
# Override senza editare il file (PowerShell):
#   $env:G1_SSH_HOST="jetson-g1"   # alias in ~/.ssh/config (es. Host jetson-g1 → User unitree, HostName …)
#   $env:G1_REMOTE_PATH="/home/unitree/G1-TalkModule-OpenAiAPI"
#   $env:G1_PUBLIC_IP="192.168.123.164"
#   $env:G1_SSH_KEY="C:\path\to\id_ed25519_jetson"   # consigliato se non usi ssh-agent
#   $env:G1_SKIP_OPENSSL="1"   # opzionale: salta generate_ssl_cert (se i certificati ci sono gia)
#   $env:G1_SSH_BATCH="0"   # chiede password SSH (se non hai chiave configurata)
#   $env:G1_SKIP_STRIP_CRLF="1"   # opzionale: salta sed CRLF su scripts/*.sh (se quel passaggio si blocca)
#   soundboard.json NON viene MAI copiato dal deploy. I suoni restano SOLO sul Jetson.
#   Backup manuale: .\scripts\pull_soundboard_from_jetson.ps1
#   .\deploy.ps1
# Backup suoni dal Jetson al PC: .\scripts\pull_soundboard_from_jetson.ps1
#
# Se lo script "si ferma" su [1]: scp sta aspettando password o conferma host (primo collegamento).
# Usa una chiave (-i / G1_SSH_KEY) o apri una finestra e accetta il fingerprint. BatchMode evita loop infiniti.

$sshHost = if ($env:G1_SSH_HOST) { $env:G1_SSH_HOST } else { "jetson-g1" }
$root = $PSScriptRoot
$remote = if ($env:G1_REMOTE_PATH) { $env:G1_REMOTE_PATH } else { "/home/unitree/G1-TalkModule-OpenAiAPI" }
$publicIp = if ($env:G1_PUBLIC_IP) { $env:G1_PUBLIC_IP } else { "192.168.123.164" }

# Opzioni SSH comuni: timeout, niente prompt password in batch (fallisce subito se manca la chiave)
$sshKeyArgs = @()
if ($env:G1_SSH_KEY) {
    $keyPath = $env:G1_SSH_KEY.Trim().Trim('"')
    if (Test-Path -LiteralPath $keyPath) {
        $sshKeyArgs = @("-i", $keyPath)
    } else {
        Write-Host "ATTENZIONE: G1_SSH_KEY non trovato: $keyPath" -ForegroundColor Yellow
    }
}
$batchRaw = if ($null -ne $env:G1_SSH_BATCH -and "$env:G1_SSH_BATCH".Trim() -ne "") { "$env:G1_SSH_BATCH".Trim() } else { "1" }
$batchMode = if ($batchRaw -eq "0") { "no" } else { "yes" }
$sshCommon = $sshKeyArgs + @(
    "-o", "ConnectTimeout=15",
    "-o", "BatchMode=$batchMode",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=6",
    "-o", "TCPKeepAlive=yes"
)
# -n: stdin da /dev/null — ok con chiave SSH. Con password (G1_SSH_BATCH=0) NON usare -n altrimenti il prompt password non appare.
$sshExec = if ($batchMode -eq "no") { $sshCommon } else { $sshCommon + @("-n") }

Write-Host "Deploy G1 Talk Module su $sshHost" -ForegroundColor Cyan
if ($env:G1_SSH_KEY) { Write-Host "  Chiave: $($env:G1_SSH_KEY)" -ForegroundColor Gray }
if ($batchMode -eq "no") {
    Write-Host "  Auth: password SSH (G1_SSH_BATCH=0) — inserisci la password quando richiesta" -ForegroundColor Yellow
} else {
    Write-Host "  Auth: chiave SSH (BatchMode). Se fallisce: `$env:G1_SSH_BATCH='0'; .\deploy.ps1" -ForegroundColor Gray
}
Write-Host ""

Write-Host "  [0] Test SSH..." -NoNewline
$preflight = & ssh @sshExec $sshHost "echo ok" 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $preflight -notmatch "ok") {
    Write-Host " FALLITO" -ForegroundColor Red
    Write-Host $preflight.Trim()
    Write-Host ""
    Write-Host "  SSH non raggiunge il Jetson. Prova manualmente:" -ForegroundColor Yellow
    Write-Host "    ssh unitree@192.168.123.164" -ForegroundColor White
    Write-Host "  Poi rilancia (stessa finestra PowerShell, senza aprire un secondo powershell.exe):" -ForegroundColor Yellow
    Write-Host "    cd `"$root`"" -ForegroundColor White
    Write-Host "    `$env:G1_SSH_BATCH='0'" -ForegroundColor White
    Write-Host "    .\deploy.ps1" -ForegroundColor White
    Write-Host "  Oppure configura una chiave: `$env:G1_SSH_KEY='C:\Users\...\.ssh\id_ed25519_jetson'" -ForegroundColor Gray
    exit 255
}
Write-Host " OK" -ForegroundColor Green
Write-Host ""

# Copia file Python
Write-Host "  [1] talk_module..." -NoNewline
scp @sshCommon `
    "$root\talk_module\web_app.py" `
    "$root\talk_module\wake.py" `
    "$root\talk_module\processing.py" `
    "$root\talk_module\config.py" `
    "$root\talk_module\openai_http.py" `
    "$root\talk_module\audio_robot_effect.py" `
    "$root\talk_module\quick_lookup.py" `
    "$root\talk_module\robot_actions.py" `
    "$root\talk_module\arm_sdk.py" `
    "$root\talk_module\teaching.py" `
    "$root\talk_module\teaching_store.py" `
    "$root\talk_module\teaching_api.py" `
    "$root\talk_module\vr_teleop.py" `
    "$root\talk_module\vr_teleop_api.py" `
    "$root\talk_module\camera_api.py" `
    "$root\talk_module\camera_yolo.py" `
    "$root\talk_module\yolo_onnx.py" `
    "$root\talk_module\pick_on_detect.py" `
    "$root\talk_module\pick_api.py" `
    "$root\talk_module\pick_adjust.py" `
    "$root\talk_module\pick_maneuver.py" `
    "$root\talk_module\hand_grasp.py" `
    "${sshHost}:${remote}/talk_module/"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE scp [1a] codice $LASTEXITCODE (SSH/chiave/host?)" -ForegroundColor Red; exit $LASTEXITCODE }
scp @sshCommon "$root\talk_module\llm\openai_client.py" "${sshHost}:${remote}/talk_module/llm/"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE scp [1a-llm] codice $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
scp @sshCommon "$root\talk_module\tts\openai_tts.py" "${sshHost}:${remote}/talk_module/tts/"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE scp [1b] codice $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
scp @sshCommon `
    "$root\talk_module\stt\fuzzy_correct.py" `
    "$root\talk_module\stt\audio_convert.py" `
    "$root\talk_module\stt\whisper_client.py" `
    "$root\talk_module\stt\groq_client.py" `
    "${sshHost}:${remote}/talk_module/stt/"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE scp [1c] codice $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
Write-Host " OK" -ForegroundColor Green

# Copia config (soundboard.json escluso: dati utente sul server, non sovrascrivere)
Write-Host "  [2] config..." -NoNewline
scp @sshCommon `
    "$root\config\knowledge.json" `
    "$root\config\visitor_profiles.json" `
    "$root\config\robot_actions.json" `
    "$root\config\stt_config.json" `
    "$root\config\italian_vocabulary.txt" `
    "$root\config\run_sheet.json" `
    "$root\config\soundboard_script.json" `
    "$root\config\elenco_testi_soundboard.txt" `
    "$root\config\pick_maneuver.json" `
    "$root\config\hand_grasp.json" `
    "${sshHost}:${remote}/config/"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE scp [2] codice $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
Write-Host " OK" -ForegroundColor Green

Write-Host "  [2b] soundboard.json... PROTETTO (non viene MAI copiato dal deploy)" -ForegroundColor DarkGreen

# Installa dipendenze (duckduckgo-search per quick_lookup)
Write-Host "  [3] Dipendenze (max 180s)..." -NoNewline
ssh @sshExec $sshHost "cd $remote && timeout 180 .venv/bin/pip install -q ddgs imageio-ffmpeg hand-tracking-sdk"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE pip $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
Write-Host " OK" -ForegroundColor Green

# Scripts e certificati SSL (3 sotto-passi: puo richiedere 15-40 s su rete lenta / Jetson)
Write-Host "  [3b] Scripts + SSL" -ForegroundColor Cyan
Write-Host "       scp (restart, generate_ssl_cert, http_redirect)..." -NoNewline
scp @sshCommon "$root\scripts\restart_server.sh" "$root\scripts\generate_ssl_cert.sh" "$root\scripts\http_redirect.py" "${sshHost}:${remote}/scripts/"
if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE scp [3b] $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
Write-Host " OK" -ForegroundColor Green
# Windows spesso salva .sh con CRLF: su bash Linux rompe "set" e path. Normalizza sul server.
Write-Host "       sed CRLF su script..." -NoNewline
if ($env:G1_SKIP_STRIP_CRLF -eq "1") {
    Write-Host " saltato (G1_SKIP_STRIP_CRLF=1)" -ForegroundColor Gray
} else {
    # `$ in doppie virgolette: solo per bash remoto. Un solo argomento remoto (niente bash -lc annidato: rompe le " su "$f").
    $stripCrlfRemote = "cd `"$remote`" && for f in scripts/*.sh; do [ -f `"`$f`" ] && sed -i 's/\r`$//' `"`$f`"; done; exit 0"
    ssh @sshExec -T $sshHost $stripCrlfRemote
    if ($LASTEXITCODE -ne 0) { Write-Host ""; Write-Host " ERRORE sed CRLF $LASTEXITCODE" -ForegroundColor Yellow }
    Write-Host " OK" -ForegroundColor Green
}
# Certificati SSL: non rilanciare openssl se i file ci sono gia (evita "blocco" percepito fino a 120s)
if ($env:G1_SKIP_OPENSSL -eq "1") {
    Write-Host "       openssl: saltato (G1_SKIP_OPENSSL=1)" -ForegroundColor Gray
} else {
    Write-Host "       openssl certificati..." -NoNewline
    ssh @sshExec -T $sshHost "cd $remote && test -f config/certs/key.pem && test -f config/certs/cert.pem"
    if ($LASTEXITCODE -eq 0) {
        Write-Host " OK (gia presenti, skip openssl)" -ForegroundColor Green
    } else {
        Write-Host " creazione (max 90s, niente output fino a fine)..." -ForegroundColor DarkGray
        ssh @sshExec -T $sshHost "cd $remote && (command -v timeout >/dev/null 2>&1 && timeout 90 env TALK_PUBLIC_HOST=$publicIp bash scripts/generate_ssl_cert.sh || env TALK_PUBLIC_HOST=$publicIp bash scripts/generate_ssl_cert.sh)"
        $certCode = $LASTEXITCODE
        if ($certCode -eq 124) {
            Write-Host " TIMEOUT openssl dopo 90s. Usa certificati esistenti o: `$env:G1_SKIP_OPENSSL='1'" -ForegroundColor Yellow
        } elseif ($certCode -ne 0) {
            Write-Host " ERRORE generate_ssl_cert $certCode" -ForegroundColor Red
            exit $certCode
        } else {
            Write-Host "       openssl: OK" -ForegroundColor Green
        }
    }
}

# Crea directory teachings sul server (per i movimenti registrati)
ssh @sshExec $sshHost "mkdir -p $remote/config/teachings"

# Backup automatico soundboard.json sul Jetson (PRIMA del restart, per sicurezza)
Write-Host "  [2c] Backup soundboard.json sul Jetson..." -NoNewline
ssh @sshExec $sshHost "cd $remote && cp -f config/soundboard.json config/soundboard.json.bak 2>/dev/null; echo ok"
Write-Host " OK" -ForegroundColor Green

# Riavvia server (timeout 50s: script ~2+8+5*2=20s max)
Write-Host "  [4] Restart server..." -ForegroundColor Gray
$keyPathResolved = if ($env:G1_SSH_KEY) { $env:G1_SSH_KEY.Trim().Trim('"') } else { "" }
$restartSshArgs = $sshKeyArgs + @(
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=10",
    "-o", "BatchMode=$batchMode",
    "-o", "StrictHostKeyChecking=accept-new",
    "-T"
)
if ($batchMode -eq "no") {
    # Foreground: permette password SSH (Start-Job non eredita la sessione interattiva)
    $r = ssh @restartSshArgs $sshHost "cd $remote && timeout 45 bash scripts/restart_server.sh" 2>&1 | Out-String
} else {
    $job = Start-Job -ScriptBlock {
        param($sshArgs, $h, $r)
        & ssh @sshArgs $h "cd $r && timeout 45 bash scripts/restart_server.sh"
    } -ArgumentList (,$restartSshArgs), $sshHost, $remote
    $null = Wait-Job $job -Timeout 50
    $r = Receive-Job $job
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
}
Write-Host $r
if ($r -match "OK:200") {
    Write-Host "  [4] HTTP 200 su /api/health" -ForegroundColor Green
} else {
    Write-Host "  [4] Problema riavvio - vedi sopra e tail /tmp/talk.log sul server" -ForegroundColor Red
}

Write-Host ""
Write-Host "  Da telefono (stessa rete):" -ForegroundColor Green
Write-Host "    http://${publicIp}:8080/client  (redirect a HTTPS)" -ForegroundColor White
Write-Host "    oppure https://${publicIp}:8081/client" -ForegroundColor White
Write-Host "  Al primo accesso: Avanzate -> Procedi (certificato)" -ForegroundColor Yellow
Write-Host "  Da PC (tunnel): http://localhost:8081/client" -ForegroundColor Cyan
Write-Host "  Ctrl+F5 per evitare cache" -ForegroundColor Gray
Write-Host ""
