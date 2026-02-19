#!/bin/bash
# Verifica se VNC è attivo sull'AI Accelerator (Jetson)
# Esegui su: lab@192.168.10.191
# Uso: bash scripts/check_vnc.sh

echo "=== Verifica VNC - AI Accelerator ==="
echo ""

# Processi VNC attivi
echo "Processi VNC:"
if pgrep -x x11vnc >/dev/null; then
    echo "  [OK] x11vnc in esecuzione"
elif pgrep -f "vncserver" >/dev/null; then
    echo "  [OK] vncserver in esecuzione"
elif pgrep -x Xtigervnc >/dev/null || pgrep -x vncserver >/dev/null; then
    echo "  [OK] TigerVNC attivo"
else
    echo "  [--] Nessun processo x11vnc/vncserver trovato"
fi
echo ""

# Porta VNC (default 5900, display :0 = 5900)
echo "Porte VNC in ascolto:"
if command -v ss >/dev/null 2>&1; then
    ss -tlnp 2>/dev/null | grep -E ':590[0-9]' || echo "  Nessuna porta 59xx in ascolto"
else
    netstat -tlnp 2>/dev/null | grep -E ':590[0-9]' || echo "  Nessuna porta 59xx in ascolto"
fi
echo ""

# Riepilogo
if pgrep -x x11vnc >/dev/null || pgrep -f "vncserver" >/dev/null; then
    echo "Risultato: VNC ATTIVO"
    exit 0
else
    echo "Risultato: VNC NON ATTIVO"
    exit 1
fi
