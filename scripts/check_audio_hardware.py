#!/usr/bin/env python3
"""
Script diagnostico: verifica presenza microfoni e altoparlanti sulla macchina.
Esegui su 192.168.10.191 via SSH per controllare l'hardware audio.
Uso: python scripts/check_audio_hardware.py
"""

import sys


def main() -> int:
    print("=" * 60)
    print("G1 Talk Module - Verifica Hardware Audio")
    print("=" * 60)

    # 1. sounddevice
    try:
        import sounddevice as sd
        print("\n[OK] sounddevice installato")
    except ImportError as e:
        print(f"\n[ERRORE] sounddevice non installato: {e}")
        print("  Esegui: pip install sounddevice")
        return 1

    # 2. Dispositivi
    try:
        devices = sd.query_devices()
    except Exception as e:
        print(f"\n[ERRORE] Impossibile interrogare dispositivi audio: {e}")
        print("  Su Linux ARM: sudo apt install portaudio19-dev")
        return 1

    inputs = [i for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    outputs = [i for i, d in enumerate(devices) if d.get("max_output_channels", 0) > 0]

    print(f"\n--- MICROFONI (input) ---")
    if not inputs:
        print("  [ATTENZIONE] Nessun microfono rilevato!")
        print("  Possibili cause:")
        print("    - Macchina headless (es. Jetson/Raspberry senza periferiche USB)")
        print("    - Driver audio non installati")
        print("    - Microfono USB non collegato")
    else:
        for i in inputs:
            d = sd.query_devices(i)
            default = " [DEFAULT]" if i == sd.default.device[0] else ""
            print(f"  {i}: {d.get('name', '?')} (ch: {d.get('max_input_channels')}, sr: {d.get('default_samplerate')}){default}")

    print(f"\n--- ALTOPARLANTI (output) ---")
    if not outputs:
        print("  [ATTENZIONE] Nessun output audio rilevato!")
        print("  Possibili cause:")
        print("    - Macchina senza audio (headless)")
        print("    - ALSA/PulseAudio non configurato")
    else:
        for i in outputs:
            d = sd.query_devices(i)
            default = " [DEFAULT]" if i == sd.default.device[1] else ""
            print(f"  {i}: {d.get('name', '?')} (ch: {d.get('max_output_channels')}){default}")

    # 3. Player esterni
    import shutil
    players = [cmd for cmd in ["ffplay", "aplay", "paplay", "mpg123"] if shutil.which(cmd)]
    print(f"\n--- Riproduttori disponibili ---")
    if players:
        print("  " + ", ".join(players))
    else:
        print("  [ATTENZIONE] Nessun player trovato (ffplay, aplay, paplay, mpg123)")
        print("  Per TTS: sudo apt install ffmpeg  (o mpg123)")

    # Riepilogo
    print("\n" + "=" * 60)
    if inputs and outputs:
        print("RISULTATO: Hardware audio presente - puoi testare localmente.")
    elif inputs and not outputs:
        print("RISULTATO: Solo microfono. Per sentire TTS serve:")
        print("  - Altoparlante/cuffie via jack/USB, oppure")
        print("  - Test da device esterno (telefono collegato alla rete)")
    elif not inputs and not outputs:
        print("RISULTATO: Macchina senza audio integrato.")
        print("  Opzioni:")
        print("  1. Collegare microfono USB + speaker USB/Jack")
        print("  2. Usare device esterno (telefono) con web interface")
        print("  3. API REST: invia audio da client remoto, ricevi risposta")
    else:
        print("RISULTATO: Solo output audio.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
