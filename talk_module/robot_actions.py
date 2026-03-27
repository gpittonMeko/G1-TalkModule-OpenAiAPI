"""
Routing azioni Unitree G1: comandi vocali -> SDK (Arm Actions, Locomotion).
Config: config/robot_actions.json. Opzionale: UNITREE_ROBOT_IP in .env.
Per SDK: pip install unitree_sdk2_python. Robot in sport mode (L1+A).

G1 Arm Action API (unitree_sdk2):
  - API 7106: ExecuteAction(action_id)
  - API 7107: GetActionList()
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

ROBOT_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "robot_actions.json"
SCRIPT_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "robot_action.sh"

G1_ARM_ACTIONS: dict[int, dict] = {
    99: {"name": "release_arm",   "label": "Rilascia braccia",       "icon": "\u23f9\ufe0f"},
    11: {"name": "two_hand_kiss", "label": "Bacio (due mani)",       "icon": "\U0001f48b"},
    12: {"name": "kiss",          "label": "Bacio (mano)",           "icon": "\U0001f618"},
    15: {"name": "hands_up",      "label": "Mani in alto",           "icon": "\U0001f64c"},
    17: {"name": "clap",          "label": "Applauso",               "icon": "\U0001f44f"},
    18: {"name": "high_five",     "label": "High Five",              "icon": "\u270b"},
    19: {"name": "hug",           "label": "Abbraccio",              "icon": "\U0001f917"},
    20: {"name": "heart",         "label": "Cuore (due mani)",       "icon": "\u2764\ufe0f"},
    21: {"name": "right_heart",   "label": "Cuore (mano dx)",        "icon": "\U0001f49c"},
    22: {"name": "reject",        "label": "Rifiuto / No",           "icon": "\U0001f645"},
    23: {"name": "right_hand_up", "label": "Mano destra su",         "icon": "\u261d\ufe0f"},
    24: {"name": "x_ray",         "label": "Braccia incrociate (X)", "icon": "\u274c"},
    25: {"name": "face_wave",     "label": "Ciao (viso)",            "icon": "\U0001f44b"},
    26: {"name": "high_wave",     "label": "Saluto alto",            "icon": "\U0001f596"},
    27: {"name": "shake_hand",    "label": "Stretta di mano",        "icon": "\U0001f91d"},
}

_NAME_TO_ID = {v["name"]: k for k, v in G1_ARM_ACTIONS.items()}

_sdk_client = None
_loco_client = None
_dds_inited = False
_sdk_lock = threading.Lock()


def _ensure_dds_init() -> None:
    """Inizializza DDS una sola volta (condivisa tra braccia e locomozione G1)."""
    global _dds_inited
    if _dds_inited:
        return
    from unitree_sdk2py.core.channel import ChannelFactory

    ChannelFactory.Instance().Init(0, "eth0")
    _dds_inited = True


def _load_robot_actions() -> dict:
    """Carica config/robot_actions.json."""
    if not ROBOT_ACTIONS_PATH.exists():
        return {}
    try:
        data = json.loads(ROBOT_ACTIONS_PATH.read_text(encoding="utf-8"))
        return {k: v for k, v in (data or {}).items() if not k.startswith("_") and isinstance(v, dict)}
    except Exception:
        return {}


def get_arm_actions_list() -> list[dict]:
    """Lista azioni braccio G1 con id, name, label, icon."""
    return [{"id": k, **v} for k, v in sorted(G1_ARM_ACTIONS.items()) if k != 99]


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


def _resolve_action_int(action_id) -> Optional[int]:
    """Risolve action_id (str name o int) in ID numerico G1."""
    if isinstance(action_id, int):
        return action_id if action_id in G1_ARM_ACTIONS else None
    s = str(action_id).strip()
    try:
        n = int(s)
        return n if n in G1_ARM_ACTIONS else None
    except ValueError:
        pass
    return _NAME_TO_ID.get(s)


def execute_robot_action(action_id: str, robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """
    Esegue azione sul robot G1. Ritorna (success, message).
    action_id: nome (shake_hand, high_wave, ...) o int (27, 26, ...).
    Prova: 1) script scripts/robot_action.sh, 2) unitree_sdk2py G1ArmActionClient.
    """
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")

    if SCRIPT_ACTIONS_PATH.exists() and os.access(SCRIPT_ACTIONS_PATH, os.X_OK):
        try:
            r = subprocess.run(
                [str(SCRIPT_ACTIONS_PATH), str(action_id), ip],
                capture_output=True, text=True, timeout=15,
                cwd=str(SCRIPT_ACTIONS_PATH.parent),
            )
            if r.returncode == 0:
                return True, "ok"
            return False, (r.stderr or r.stdout or "script fallito").strip()
        except Exception as e:
            return False, str(e)

    act_int = _resolve_action_int(action_id)
    if act_int is None:
        if str(action_id).startswith("teaching_"):
            return False, f"Teaching: crea scripts/robot_action.sh con logica per {action_id}"
        return False, f"Azione non riconosciuta: {action_id}"

    return _do_arm_action(act_int, ip)


def _do_arm_action(action_id: int, robot_ip: str) -> tuple[bool, str]:
    """Esegue G1 Arm Action via SDK (API 7106 = ExecuteAction)."""
    global _sdk_client
    try:
        from unitree_sdk2py.core.channel import ChannelFactorytInitialize
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient
    except ImportError:
        pass

    try:
        from unitree_sdk2py.core.channel import ChannelFactory
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient

        with _sdk_lock:
            _ensure_dds_init()
            if _sdk_client is None:
                _sdk_client = G1ArmActionClient()
                _sdk_client.Init()
                _sdk_client.SetTimeout(10.0)
            ret = _sdk_client.ExecuteAction(action_id)
        if ret == 0:
            return True, "ok"
        err_map = {7400: "rt/armsdk occupato da altro processo", 7401: "braccio occupato (invia release 99)",
                   7402: "action_id non valido", 7404: "FSM state non compatibile (serve sport mode L1+A)"}
        return False, err_map.get(ret, f"errore SDK rc={ret}")
    except ImportError:
        return _do_arm_action_http(action_id, robot_ip)
    except Exception as e:
        return False, f"SDK error: {e}"


def _do_arm_action_http(action_id: int, robot_ip: str) -> tuple[bool, str]:
    """Fallback HTTP: chiama endpoint Unitree se disponibile, altrimenti errore."""
    import urllib.request
    url = f"http://{robot_ip}:8081/api/sport/request"
    payload = json.dumps({"api_id": 7106, "parameter": json.dumps({"data": action_id})}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return True, body[:200]
    except Exception:
        return False, "unitree_sdk2py non installato e HTTP fallback non disponibile. pip install unitree_sdk2_python"


def send_move_command(vx: float, vy: float, vyaw: float, robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """G1: velocità via LocoClient.Move (vx avanti, vy laterale, vyaw rad/s)."""
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    global _loco_client
    try:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        with _sdk_lock:
            _ensure_dds_init()
            if _loco_client is None:
                _loco_client = LocoClient()
                _loco_client.SetTimeout(10.0)
                _loco_client.Init()
            _loco_client.Move(float(vx), float(vy), float(vyaw))
        return True, "ok"
    except ImportError:
        pass
    except Exception as e:
        return False, f"G1 Move: {e}"
    import urllib.request

    url = f"http://{ip}:8081/api/sport/request"
    params = json.dumps({"x": vx, "y": vy, "z": vyaw})
    payload = json.dumps({"api_id": 1008, "parameter": params}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return True, resp.read().decode()[:200]
    except Exception as e:
        return False, f"move HTTP fallback: {e}"


def execute_g1_loco_command(command: str, robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """
    Comandi locomozione G1 (LocoClient, es. g1_loco_client_example.py):
      ready → HighStand (posa eretta / pronto)
      walk  → Move(0.28, 0, 0) passo avanti (stesso schema SDK)
      stop_walk → Move(0, 0, 0)
    """
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    cmd = (command or "").strip().lower()

    if SCRIPT_ACTIONS_PATH.exists() and os.access(SCRIPT_ACTIONS_PATH, os.X_OK):
        try:
            r = subprocess.run(
                [str(SCRIPT_ACTIONS_PATH), f"loco_{cmd}", ip],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=str(SCRIPT_ACTIONS_PATH.parent),
            )
            if r.returncode == 0:
                return True, "ok"
        except Exception:
            pass

    global _loco_client
    try:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        with _sdk_lock:
            _ensure_dds_init()
            if _loco_client is None:
                _loco_client = LocoClient()
                _loco_client.SetTimeout(10.0)
                _loco_client.Init()
            if cmd in ("ready", "pronto", "high_stand"):
                _loco_client.HighStand()
            elif cmd in ("low_stand", "basso"):
                _loco_client.LowStand()
            elif cmd in ("walk", "cammina", "avanti"):
                _loco_client.Move(0.28, 0.0, 0.0)
            elif cmd in ("stop_walk", "stop", "ferma"):
                _loco_client.Move(0.0, 0.0, 0.0)
            else:
                return False, f"Comando locomozione sconosciuto: {command}"
        return True, "ok"
    except ImportError:
        return False, "Installa unitree_sdk2_python con modulo G1 LocoClient"
    except Exception as e:
        return False, str(e)
