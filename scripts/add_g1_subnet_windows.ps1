# Aggiunge un IP secondario sulla subnet Unitree (192.168.123.x) mantenendo il DHCP sul TP-Link (192.168.1.x).
# Esegui in PowerShell: tasto destro -> Esegui come amministratore
# Uso: .\scripts\add_g1_subnet_windows.ps1
# Rimuovi con: .\scripts\add_g1_subnet_windows.ps1 -Remove

param(
    [string] $InterfaceName = "Wi-Fi",
    [string] $Address = "192.168.123.200",
    [string] $Mask = "255.255.255.0",
    [switch] $Remove
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host "ERRORE: esegui questo script come Amministratore (tasto destro su PowerShell)." -ForegroundColor Red
    exit 1
}

if ($Remove) {
    netsh interface ipv4 delete address "$InterfaceName" $Address
    Write-Host "Rimosso $Address da $InterfaceName" -ForegroundColor Green
    exit 0
}

$exists = netsh interface ipv4 show addresses "$InterfaceName" 2>$null | Select-String $Address
if ($exists) {
    Write-Host "Indirizzo $Address gia presente su $InterfaceName" -ForegroundColor Yellow
} else {
    netsh interface ipv4 add address "$InterfaceName" $Address $Mask
    Write-Host "Aggiunto $Address/$Mask su $InterfaceName" -ForegroundColor Green
}

Write-Host ""
Write-Host "Prova ping verso il robot (IP tipici Unitree):" -ForegroundColor Cyan
foreach ($ip in @("192.168.123.161", "192.168.123.164", "192.168.123.18")) {
    $r = Test-Connection -ComputerName $ip -Count 1 -Quiet -ErrorAction SilentlyContinue
    if ($r) { Write-Host "  OK  $ip" -ForegroundColor Green }
    else { Write-Host "  --- $ip (nessuna risposta)" -ForegroundColor DarkGray }
}
Write-Host ""
Write-Host "Se tutti falliscono: il G1 sulla porta LAN del TP-Link potrebbe avere IP 192.168.1.x (vedi client DHCP sul router)." -ForegroundColor Gray
Write-Host "Per rimuovere l'IP extra: .\scripts\add_g1_subnet_windows.ps1 -Remove" -ForegroundColor Gray
