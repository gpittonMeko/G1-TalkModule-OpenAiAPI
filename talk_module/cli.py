"""
CLI per G1 Talk Module.
Uso: python -m talk_module.cli [comando]
"""

import argparse
import sys

from talk_module.config import settings
from talk_module.pipeline import TalkPipeline
from talk_module.audio import list_audio_devices


def cmd_list_devices() -> int:
    """Lista dispositivi audio disponibili."""
    print("\n=== Dispositivi Audio ===\n")
    devices = list_audio_devices()
    for d in devices:
        if d["input_channels"] > 0:
            marker = " [DEFAULT INPUT]" if d["is_default_input"] else ""
            print(f"  {d['index']}: {d['name']}{marker}")
            print(f"      Input channels: {d['input_channels']}, Sample rate: {d['sample_rate']}")
    print("\nImposta MICROPHONE_DEVICE_ID nel .env per usare un microfono specifico.\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Avvia il Talk Pipeline."""
    errs = settings.validate()
    if errs:
        for e in errs:
            print(f"[ERRORE] {e}")
        print("\nCopia .env.example in .env e configuralo.")
        return 1
    pipeline = TalkPipeline(
        microphone_id=args.device,
        sample_rate=args.sample_rate,
    )
    if args.once:
        pipeline.run_once(args.duration)
    else:
        pipeline.run_conversation(duration_seconds=args.duration)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Test rapido: solo STT o solo TTS."""
    errs = settings.validate()
    if errs:
        for e in errs:
            print(f"[ERRORE] {e}")
        return 1
    if args.mode == "stt":
        from talk_module.audio import AudioRecorder
        from talk_module.stt import WhisperClient
        print("Registrazione 5 secondi...")
        rec = AudioRecorder(device_id=args.device)
        audio = rec.record_fixed_duration(5)
        text = WhisperClient().transcribe(audio)
        print(f"Trascritto: {text or '(vuoto)'}")
    elif args.mode == "tts":
        from talk_module.tts import TTSClient
        from talk_module.audio import AudioPlayer
        text = args.text or "Questo è un test di sintesi vocale."
        audio = TTSClient().synthesize(text)
        AudioPlayer().play_bytes(audio, format_hint="mp3")
        print("Riproduzione completata.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="talk-module",
        description="G1 Talk Module - Voce -> STT -> LLM -> TTS",
    )
    sub = parser.add_subparsers(dest="cmd", help="Comando")

    # list-devices
    sub.add_parser("list-devices", help="Lista microfoni e dispositivi audio")

    # run
    p_run = sub.add_parser("run", help="Avvia conversazione vocale")
    p_run.add_argument("--once", action="store_true", help="Un solo turno")
    p_run.add_argument("-d", "--duration", type=float, default=None, help="Secondi registrazione")
    p_run.add_argument("--device", type=int, default=None, help="ID microfono")
    p_run.add_argument("--sample-rate", type=int, default=None, help="Sample rate")

    # test
    p_test = sub.add_parser("test", help="Test STT o TTS")
    p_test.add_argument("mode", choices=["stt", "tts"], help="stt o tts")
    p_test.add_argument("--text", type=str, default=None, help="Per TTS: testo da sintetizzare")
    p_test.add_argument("--device", type=int, default=None, help="ID microfono (per STT)")

    args = parser.parse_args()

    if args.cmd == "list-devices":
        return cmd_list_devices()
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "test":
        return cmd_test(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
