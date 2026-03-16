"""
Routing azioni Unitree G1: comandi vocali -> SDK (ShakeHand, WaveHand, Teaching).
Config: config/robot_actions.json. Opzionale: UNITREE_ROBOT_IP in .env.
Per SDK: pip install unitree_sdk2_python. Robot in sport mode (L1+A).
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

ROBOT_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "robot_actions.json"
SCRIPT_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "robot_action.sh"


def _load_robot_actions() -> dict:
    """Carica config/robot_actions.json."""
    if not ROBOT_ACTIONS_PATH.exists():
        return {}
    try:
        data = json.loads(ROBOT_ACTIONS_PATH.read_text(encoding="utf-8"))
        return {k: v for k, v in (data or {}).items() if not k.startswith("_") and isinstance(v, dict)}
    except Exception:
        return {}


def check_robot_action(user_input: str) -> Optional[tuple[str, str]]:
    """
    Se user_input contiene un pattern di robot_actions, ritorna (response, action_id).
    Altrimenti None.
    """
    if not user_input or not user_input.strip():
        return None
    txt = user_input.strip().lower()
    actions = _load_robot_actions()
    for pattern, cfg in sorted(actions.items(), key=lambda x: -len(x[0])):
        if pattern and pattern in txt:
            action_id = (cfg.get("action") or "").strip()
            response = (cfg.get("response") or "Ok").strip()
            if action_id:
                return response, action_id
    return None


def execute_robot_action(action_id: str, robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """
    Esegue azione sul robot G1. Ritorna (success, message).
    action_id: shake_hand, wave_hand, teaching_1, teaching_2, ...
    Prova: 1) script scripts/robot_action.sh, 2) unitree_sdk2 se installato.
    """
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "")

    # 1. Script esterno scripts/robot_action.sh (opzionale)
    if SCRIPT_ACTIONS_PATH.exists() and os.access(SCRIPT_ACTIONS_PATH, os.X_OK):
        try:
            r = subprocess.run(
                [str(SCRIPT_ACTIONS_PATH), action_id, ip],
                capture_output=True, text=True, timeout=10,
                cwd=str(SCRIPT_ACTIONS_PATH.parent),
            )
            if r.returncode == 0:
                return True, "ok"
            return False, (r.stderr or r.stdout or "script fallito").strip()
        except Exception as e:
            return False, str(e)

    # 2. SDK Python unitree_sdk2 (se installato)
    if action_id == "shake_hand":
        return _do_sdk_action(7106, ip)
    if action_id == "wave_hand":
        return _do_sdk_action(7107, ip)
    if action_id.startswith("teaching_"):
        return False, f"Teaching: aggiungi {action_id} in scripts/robot_action.sh (ID dalla app Unitree)"

    return False, f"Azione non supportata: {action_id}"


def _do_sdk_action(api_id: int, robot_ip: str) -> tuple[bool, str]:
    """Chiama API sport via unitree_sdk2 (ShakeHand=7106, WaveHand=7107)."""
    try:
        from unitree_sdk2py.core.channel import ChannelPublisher
        from unitree_sdk2py.idl.unitree.api.v1 import Request
        req = Request()
        req.header.identity.id = api_id
        pub = ChannelPublisher("sport", "Request")
        pub.write(req)
        return True, "ok"
    except ImportError:
        return False, "unitree_sdk2 non installato. Oppure crea scripts/robot_action.sh"
    except Exception as e:
        return False, str(e)
