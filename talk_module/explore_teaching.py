"""Unitree Explore app teachings: list and play movements stored on the robot."""

from __future__ import annotations

import os
import threading
from typing import Optional


def list_explore_teachings(robot_ip: Optional[str] = None) -> dict:
    """Return custom teachings from the Explore app (SDK GetActionList / API 7107)."""
    _ = robot_ip
    from talk_module.robot_actions import fetch_unitree_robot_action_catalog

    catalog = fetch_unitree_robot_action_catalog()
    if not catalog.get("ok"):
        return {
            "ok": False,
            "error": catalog.get("error") or "Elenco non disponibile",
            "teachings": [],
            "presets": catalog.get("preset") or [],
        }
    teachings = []
    for item in catalog.get("custom") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        entry = {"name": name, "source": "explore_app"}
        if item.get("duration_s") is not None:
            entry["duration_s"] = item["duration_s"]
        teachings.append(entry)
    teachings.sort(key=lambda row: row["name"].lower())
    return {
        "ok": True,
        "teachings": teachings,
        "presets": catalog.get("preset") or [],
        "error": "",
    }


def play_explore_teaching(name: str, robot_ip: Optional[str] = None) -> dict:
    """Play a custom teaching by exact name (SDK API 7108)."""
    from talk_module.robot_actions import execute_unitree_custom_teaching

    action_name = (name or "").strip()
    if not action_name:
        return {"ok": False, "message": "nome movimento richiesto", "name": ""}
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    ok, msg = execute_unitree_custom_teaching(action_name, robot_ip=ip)
    return {"ok": ok, "message": msg, "name": action_name}


def stop_explore_teaching(robot_ip: Optional[str] = None) -> dict:
    """Stop custom teach (7113) and release arm hold."""
    from talk_module.robot_actions import stop_unitree_custom_teaching

    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    ok, msg = stop_unitree_custom_teaching(robot_ip=ip)
    return {"ok": ok, "message": msg}


def explore_teaching_status(robot_ip: Optional[str] = None) -> dict:
    """Diagnostics: FSM, arm_sdk lock, catalog availability."""
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    out: dict = {"ok": True, "robot_ip": ip}
    try:
        from talk_module.arm_sdk import is_arm_sdk_active

        out["arm_sdk_active"] = is_arm_sdk_active()
    except ImportError:
        out["arm_sdk_active"] = False
    try:
        from talk_module.robot_actions import probe_g1_sport_status

        out["sport"] = probe_g1_sport_status(robot_ip=ip)
    except Exception as e:
        out["sport"] = {"sport_status": "error", "detail": str(e)}
    catalog = list_explore_teachings(robot_ip=ip)
    out["catalog_ok"] = bool(catalog.get("ok"))
    out["teaching_count"] = len(catalog.get("teachings") or [])
    out["catalog_error"] = catalog.get("error") or ""
    return out


def play_explore_teaching_async(name: str, robot_ip: Optional[str] = None) -> None:
    """Fire-and-forget playback (soundboard / voice hooks)."""

    def _run() -> None:
        result = play_explore_teaching(name, robot_ip=robot_ip)
        print(f"[explore-teach] play {name!r} -> {result}", flush=True)

    threading.Thread(target=_run, daemon=True).start()
