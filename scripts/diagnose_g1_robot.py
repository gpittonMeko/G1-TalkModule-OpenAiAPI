#!/usr/bin/env python3
"""Diagnostica G1 da Jetson: rete, SDK, FSM, gesto braccio.
Uso: cd ~/G1-TalkModule-OpenAiAPI && .venv/bin/python3 scripts/diagnose_g1_robot.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

ROBOT_IP = os.getenv("UNITREE_ROBOT_IP", "192.168.123.161").strip()


def step(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def run_ping() -> bool:
    step("1) Ping robot")
    print(f"Target: {ROBOT_IP}")
    r = subprocess.run(["ping", "-c", "2", "-W", "2", ROBOT_IP], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    ok = r.returncode == 0
    print("OK" if ok else "FAIL — nessuna risposta dal corpo robot")
    return ok


def run_sdk_import() -> bool:
    step("2) SDK Python")
    try:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient  # noqa: F401
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient  # noqa: F401

        print("unitree_sdk2py: import OK")
        return True
    except ImportError as e:
        print(f"FAIL: {e}")
        print("Fix: bash scripts/install_unitree_sdk_jetson.sh")
        return False


def run_iface() -> str:
    step("3) Interfaccia DDS")
    from talk_module.robot_actions import _dds_interface_for_init

    iface = _dds_interface_for_init()
    print(f"UNITREE_DDS_INTERFACE env = {os.getenv('UNITREE_DDS_INTERFACE', '(unset)')!r}")
    print(f"Interfaccia scelta        = {iface!r}")
    out = subprocess.run(["ip", "-br", "addr", "show", iface], capture_output=True, text=True)
    print(out.stdout or out.stderr or "(ip non disponibile)")
    return iface


def run_loco_fsm(iface: str) -> None:
    step("4) LocoClient — lettura / cambio FSM")
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
    from talk_module.robot_actions import _read_loco_fsm

    try:
        ChannelFactoryInitialize(0, iface)
    except ImportError:
        from unitree_sdk2py.core.channel import ChannelFactory

        ChannelFactory.Instance().Init(0, iface)

    lc = LocoClient()
    lc.SetTimeout(10.0)
    lc.Init()

    fsm_before, mode_before = _read_loco_fsm(lc)
    print(f"FSM prima: id={fsm_before} mode={mode_before}")

    rc500 = lc.SetFsmId(500)
    time.sleep(1.2)
    fsm_after, mode_after = _read_loco_fsm(lc)
    print(f"SetFsmId(500) rc={rc500}")
    print(f"FSM dopo:  id={fsm_after} mode={mode_after}")

    if fsm_after not in (500, 501, 801):
        if fsm_after == fsm_before and fsm_before in (802, 803):
            print(
                f"BLOCCO: FSM {fsm_before} — modalità AI/app. SetFsmId non ha effetto.\n"
                "      Esci da AI mode sul telecomando, poi L1+A (sport mode)."
            )
        else:
            print(
                "WARN: FSM non valido per gesti braccia (serve 500/501/801).\n"
                "      Sul telecomando Unitree: accensione → L1+A (sport mode), poi riprova."
            )
    else:
        print("FSM OK per gesti braccia")


def run_arm_action(iface: str) -> None:
    step("5) Gesto test — mano destra su (id 23)")
    print("Tra 2 secondi invio ExecuteAction(23)...")
    time.sleep(2)

    from talk_module.robot_actions import execute_robot_action

    ok, msg = execute_robot_action(23, robot_ip=ROBOT_IP)
    print(f"Risultato: ok={ok}")
    print(f"Messaggio: {msg}")
    if ok:
        print("Se il braccio NON si è mosso: sport mode (L1+A) o robot in debug mode.")
    else:
        print("Controlla messaggio errore sopra (7404 = FSM, 7400 = teaching occupato).")


def main() -> int:
    print("G1 diagnose — Jetson → robot via DDS")
    print(f"ROBOT_IP={ROBOT_IP}")

    if not run_ping():
        print("\n>>> Bloccato: Jetson non raggiunge il robot. Cavo/rete/power.")
        return 1
    if not run_sdk_import():
        return 1

    iface = run_iface()
    try:
        run_loco_fsm(iface)
        run_arm_action(iface)
    except Exception as e:
        step("ERRORE")
        print(repr(e))
        print(
            "\nSe ChannelFactory / DDS fallisce: prova export UNITREE_DDS_INTERFACE=eth0 "
            "e ripeti."
        )
        return 1

    step("Fine")
    print(
        "Se tutto OK ma zero movimento:\n"
        "  1) Telecomando: L1+A (sport mode, LED/app cambia)\n"
        "  2) Robot in piedi, non in debug/developer mode bloccato\n"
        "  3) App ufficiale Unitree: prova un gesto — se anche lì niente, hardware/mode\n"
        "  4) tail -50 /tmp/talk.log durante click da web UI"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
