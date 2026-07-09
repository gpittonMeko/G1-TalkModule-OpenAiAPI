#!/usr/bin/env python3
"""Verifica dipendenze Jetson G1 dopo installazione completa."""
from __future__ import annotations

import sys


def _check(name: str, fn) -> bool:
    try:
        fn()
        print(f"  [OK] {name}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return False


def main() -> int:
    fails = 0
    print("=== G1 Talk — verifica dipendenze Jetson ===\n")

    fails += 0 if _check("talk_module.web_app", lambda: __import__("talk_module.web_app")) else 1
    fails += 0 if _check("opencv (cv2)", lambda: __import__("cv2")) else 1
    fails += 0 if _check("numpy", lambda: __import__("numpy")) else 1

    def _yolo():
        from talk_module.yolo_onnx import ensure_onnx_model, default_onnx_model_path

        ensure_onnx_model(default_onnx_model_path())

    fails += 0 if _check("YOLO ONNX model", _yolo) else 1

    def _sdk_loco():
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient  # noqa: F401

    fails += 0 if _check("unitree_sdk2py LocoClient", _sdk_loco) else 1

    def _sdk_arm():
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient  # noqa: F401

    fails += 0 if _check("unitree_sdk2py G1ArmActionClient", _sdk_arm) else 1

    def _sdk_audio():
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient  # noqa: F401

    fails += 0 if _check("unitree_sdk2py AudioClient", _sdk_audio) else 1

    try:
        import pyrealsense2 as rs  # noqa: F401

        print(f"  [OK] pyrealsense2 (opzionale)")
    except Exception:
        print("  [SKIP] pyrealsense2 — opzionale; bash scripts/install_realsense_jetson.sh")

    print(f"\n=== Fine ({fails} errori critici) ===")
    if fails:
        print("Fix: bash scripts/install_jetson_completo.sh")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
