"""
Routing azioni Unitree G1: comandi vocali -> SDK (Arm Actions, Locomotion).
Config: config/robot_actions.json. Opzionale: UNITREE_ROBOT_IP in .env.
Per SDK: unitree_sdk2py. Robot in sport mode (L1+A).

DDS: SDK recente usa ChannelFactoryInitialize(0, iface); le versioni vecchie
usano ChannelFactory.Instance().Init(...) — vedi _ensure_dds_init().
"""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RobotMatch:
    """Match da config/robot_actions.json: risposta TTS + azione braccia e/o comando locomozione."""

    response: str
    arm_action: str = ""
    loco_command: str = ""
    led_effect: str = ""

# Comandi che mettono il G1 in smorzamento / coppia zero o sequenze da caduta: richiedono confirmed=true (client «Sei sicuro?»).
_LOCO_REQUIRES_CONFIRM = frozenset(
    {
        "damp",
        "zero_torque",
        "squat_up_damp",
        "lie_standup",
    }
)


def loco_command_requires_confirm(command: str) -> bool:
    return (command or "").strip().lower() in _LOCO_REQUIRES_CONFIRM


def _loco_log(msg: str) -> None:
    print(f"[G1 loco {datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)

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
_NAME_TO_ID["wave_hand"] = 25  # alias → face_wave
_SHAKE_HAND_ID = _NAME_TO_ID.get("shake_hand", 27)

_sdk_client = None
_loco_client = None
_audio_client = None
_dds_inited = False
_bound_loco_service: Optional[str] = None
_dds_init_lock = threading.Lock()
_arm_sdk_lock = threading.Lock()
_audio_sdk_lock = threading.Lock()
_sdk_lock = _arm_sdk_lock  # compat: braccia/loco


def _dds_interface_for_init() -> str:
    """
    Interfaccia CycloneDDS verso il G1.
    Priorità: 1) UNITREE_DDS_INTERFACE da .env  2) auto-detect interfaccia con IP 192.168.123.x  3) eth0.
    """
    explicit = (os.getenv("UNITREE_DDS_INTERFACE") or "").strip()
    if explicit and explicit != "auto":
        return explicit
    target_subnet = os.getenv("UNITREE_ROBOT_IP", "192.168.123.161").rsplit(".", 1)[0] + "."
    try:
        import subprocess as _sp
        out = _sp.check_output(["ip", "-br", "addr", "show"], text=True, timeout=5)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            iface, state = parts[0], parts[1]
            if state not in ("UP", "UNKNOWN"):
                continue
            for addr in parts[2:]:
                if addr.startswith(target_subnet):
                    return iface
    except Exception:
        pass
    return "eth0"


def _reset_dds_state() -> None:
    """Dopo errore channel factory / init DDS: permette un nuovo tentativo (es. passaggio a usb0)."""
    global _dds_inited, _sdk_client, _loco_client
    with _sdk_lock:
        _dds_inited = False
        _sdk_client = None
        _loco_client = None


def _ensure_dds_init() -> None:
    """Inizializza DDS una sola volta (braccia + locomozione G1)."""
    global _dds_inited
    if _dds_inited:
        return
    with _dds_init_lock:
        if _dds_inited:
            return
        iface = _dds_interface_for_init()
        print(f"[G1 DDS] ChannelFactoryInitialize(0, {iface!r})", flush=True)
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize

            ChannelFactoryInitialize(0, iface)
        except ImportError:
            from unitree_sdk2py.core.channel import ChannelFactory

            ChannelFactory.Instance().Init(0, iface)
        _dds_inited = True


def _ensure_loco_client_locked():
    """
    Con _sdk_lock acquisito. DDS già inizializzato.
    Applica UNITREE_LOCO_SERVICE_NAME (default sport; su firmware vecchi prova loco).
    """
    global _loco_client, _bound_loco_service
    import unitree_sdk2py.g1.loco.g1_loco_api as g1_api
    import unitree_sdk2py.g1.loco.g1_loco_client as g1_lc_mod
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    name = (os.getenv("UNITREE_LOCO_SERVICE_NAME") or "sport").strip() or "sport"
    g1_api.LOCO_SERVICE_NAME = name
    if hasattr(g1_lc_mod, "LOCO_SERVICE_NAME"):
        g1_lc_mod.LOCO_SERVICE_NAME = name
    if _loco_client is not None and _bound_loco_service != name:
        _loco_client = None
    _bound_loco_service = name
    if _loco_client is None:
        _loco_client = LocoClient()
        try:
            to = float(os.getenv("UNITREE_LOCO_TIMEOUT", "10") or "10")
        except ValueError:
            to = 10.0
        _loco_client.SetTimeout(to)
        _loco_client.Init()
    return _loco_client, name


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


_ARM_ACTION_API_IDS = (7106, 7107, 7108, 7113)
_CUSTOM_TEACHING_ERR = {
    7400: "rt/arm_sdk occupato (teaching/VR attivo — premi Rilascia braccia o STOP VR)",
    7401: "braccio in hold — premi Rilascia braccia",
    7404: "FSM non compatibile: telecomando L1+A (sport mode), poi Modalità gesti",
}


def _register_arm_action_apis(client) -> None:
    for api_id in _ARM_ACTION_API_IDS:
        try:
            client._RegistApi(api_id, 0)
        except Exception:
            pass


def _ensure_g1_arm_action_client(timeout: float = 12.0):
    """Singleton G1ArmActionClient con API 7106–7113 registrate."""
    global _sdk_client
    from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient

    with _sdk_lock:
        _ensure_dds_init()
        if _sdk_client is None:
            _sdk_client = G1ArmActionClient()
            _sdk_client.Init()
        _sdk_client.SetTimeout(timeout)
        _register_arm_action_apis(_sdk_client)
    return _sdk_client


def _parse_get_action_list_payload(data) -> tuple[list, list]:
    """Normalizza la risposta GetActionList (7107) in (preset, custom)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return [], []
    if isinstance(data, dict):
        preset = data.get("preset") or data.get("preset_actions") or data.get("presets") or []
        custom = data.get("custom") or data.get("custom_actions") or data.get("teachings") or []
        if not preset and not custom:
            for key in ("data", "actions", "action_list"):
                inner = data.get(key)
                if inner is not None:
                    return _parse_get_action_list_payload(inner)
        return list(preset or []), list(custom or [])
    if isinstance(data, list):
        if len(data) >= 2 and isinstance(data[0], list) and isinstance(data[1], list):
            return data[0], data[1]
        if data and all(isinstance(item, dict) for item in data):
            preset_rows = [item for item in data if item.get("id") is not None and not item.get("time")]
            custom_rows = [item for item in data if item.get("name") and item not in preset_rows]
            if custom_rows or not preset_rows:
                return preset_rows, custom_rows if custom_rows else data
    return [], []


def _custom_teaching_err(code: int) -> str:
    return _CUSTOM_TEACHING_ERR.get(code, f"errore SDK rc={code}")


def _do_custom_teaching_sdk(action_name: str) -> int:
    """API 7108 ExecuteCustomAction — ritorna rc SDK (0 = ok)."""
    name = (action_name or "").strip()
    if not name:
        return -1
    client = _ensure_g1_arm_action_client(timeout=15.0)
    with _sdk_lock:
        parameter = json.dumps({"action_name": name}, ensure_ascii=False)
        code, _data = client._Call(7108, parameter)
    return int(code)


def stop_unitree_custom_teaching(robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """Ferma teach custom (API 7113) e rilascia hold braccia (99)."""
    _ = robot_ip
    try:
        from talk_module.arm_sdk import is_arm_sdk_active

        if is_arm_sdk_active():
            return False, "rt/arm_sdk occupato da teaching/VR — attendi o premi STOP"
    except ImportError:
        pass
    try:
        client = _ensure_g1_arm_action_client(timeout=10.0)
        with _sdk_lock:
            code, _data = client._Call(7113, "{}")
        if code == 0:
            return True, "ok — teach fermato (7113)"
        ok99, msg99 = _do_arm_action_sdk(99, robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161"))
        if ok99:
            return True, "ok — braccia rilasciate (99)"
        return False, _custom_teaching_err(code) + f"; release 99: {msg99}"
    except ImportError:
        return _do_arm_action_sdk(99, robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161"))
    except Exception as e:
        return False, f"SDK error: {e}"


def fetch_unitree_robot_action_catalog() -> dict:
    """Elenco azioni dal robot via SDK GetActionList (7107): preset + teach app Unitree."""
    try:
        from talk_module.arm_sdk import is_arm_sdk_active

        if is_arm_sdk_active():
            return {
                "ok": False,
                "error": "rt/arm_sdk occupato da teaching/VR — attendi o premi STOP",
                "custom": [],
                "preset": [],
            }
    except ImportError:
        pass
    try:
        client = _ensure_g1_arm_action_client(timeout=12.0)
        with _sdk_lock:
            code, data = client.GetActionList()
        if code != 0:
            return {"ok": False, "error": f"GetActionList rc={code}", "custom": [], "preset": []}

        preset_raw, custom_raw = _parse_get_action_list_payload(data)

        custom_out = []
        for item in custom_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            entry = {"name": name, "source": "unitree_app"}
            if item.get("time") is not None:
                try:
                    entry["duration_s"] = round(float(item["time"]), 2)
                except (TypeError, ValueError):
                    pass
            custom_out.append(entry)

        preset_out = []
        for item in preset_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            aid = item.get("id")
            if aid is None and not name:
                continue
            preset_out.append({"id": aid, "name": name, "source": "preset"})

        return {"ok": True, "custom": custom_out, "preset": preset_out, "error": ""}
    except ImportError:
        return {"ok": False, "error": "unitree_sdk2py non disponibile", "custom": [], "preset": []}
    except Exception as e:
        return {"ok": False, "error": str(e), "custom": [], "preset": []}


def execute_unitree_custom_teaching(action_name: str, robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """Riproduce un teach registrato nell'app Unitree (API 7108, action_name)."""
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    name = (action_name or "").strip()
    if not name:
        return False, "nome teach richiesto"
    try:
        from talk_module.arm_sdk import is_arm_sdk_active

        if is_arm_sdk_active():
            return False, "rt/arm_sdk occupato da teaching/VR — attendi o premi STOP"
    except ImportError:
        pass

    def _attempt() -> tuple[bool, str]:
        fsm_ok, fsm_msg = _ensure_arm_action_fsm()
        print(f"[G1 teach] FSM check: {fsm_msg}", flush=True)
        if not fsm_ok:
            return False, fsm_msg
        stop_unitree_custom_teaching(ip)
        time.sleep(0.25)
        code = _do_custom_teaching_sdk(name)
        if code == 0:
            return True, "ok"
        if code == 7401:
            _do_arm_action_sdk(99, ip)
            time.sleep(0.35)
            code = _do_custom_teaching_sdk(name)
            if code == 0:
                return True, "ok"
        return False, _custom_teaching_err(code)

    try:
        ok, msg = _attempt()
        if (
            not ok
            and Path("/sys/class/net/usb0").exists()
            and "channel factory" in (msg or "").lower()
            and (os.getenv("UNITREE_DDS_INTERFACE") or "").strip().lower() != "usb0"
        ):
            print("[G1 DDS] retry explore teaching with UNITREE_DDS_INTERFACE=usb0", flush=True)
            os.environ["UNITREE_DDS_INTERFACE"] = "usb0"
            _reset_dds_state()
            ok, msg = _attempt()
        return ok, msg
    except ImportError:
        return False, "unitree_sdk2py non disponibile"
    except Exception as e:
        return False, f"SDK error: {e}"

def _phrase_in_text(text: str, pattern: str) -> bool:
    """Whole-phrase match with word boundaries (avoids 'mani su' inside 'mani sulla')."""
    pat = re.escape((pattern or "").strip().lower())
    if not pat:
        return False
    return bool(re.search(rf"(?<!\w){pat}(?!\w)", (text or "").strip().lower()))


def _fuzzy_contains(text: str, pattern: str, threshold: float = 0.82) -> bool:
    """True se text contiene pattern esattamente, oppure una sotto-sequenza molto simile."""
    if _phrase_in_text(text, pattern):
        return True
    pwords = pattern.split()
    if len(pwords) < 2:
        return False
    from difflib import SequenceMatcher
    plen = len(pattern)
    margin = max(3, plen // 4)
    text_l = (text or "").strip().lower()
    for start in range(0, max(1, len(text_l) - plen + margin + 1)):
        end = min(len(text_l), start + plen + margin)
        window = text_l[start:end]
        if SequenceMatcher(None, window, pattern).ratio() >= threshold:
            left_ok = start == 0 or not text_l[start - 1].isalnum()
            match_end = start + len(window.rstrip())
            right_ok = match_end >= len(text_l) or not text_l[match_end].isalnum()
            if left_ok and right_ok:
                return True
    return False


def check_robot_action(user_input: str) -> Optional[RobotMatch]:
    """
    Se user_input contiene (o fuzzy-contiene) un pattern di robot_actions, ritorna RobotMatch.
    Altrimenti None.
    """
    if not user_input or not user_input.strip():
        return None
    txt = user_input.strip().lower()
    actions = _load_robot_actions()
    for pattern, cfg in sorted(actions.items(), key=lambda x: -len(x[0])):
        if not pattern:
            continue
        if _fuzzy_contains(txt, pattern):
            arm = (cfg.get("action") or "").strip()
            loco = (cfg.get("loco_command") or cfg.get("loco") or "").strip()
            response = (cfg.get("response") or "Ok").strip()
            if arm or loco:
                le = str(cfg.get("led_effect") or "").strip()
                return RobotMatch(response=response, arm_action=arm, loco_command=loco, led_effect=le)
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


# Gesti che richiedono entrambe le braccia (tempo più lungo prima del release automatico).
_BOTH_ARMS_ACTION_IDS = frozenset({11, 15, 17, 19, 20, 24})

_ARM_RELEASE_DELAYS = {
    11: 9.0,   # bacio 2 mani
    15: 9.0,   # mani in alto
    17: 7.0,   # applauso
    19: 9.0,   # abbraccio
    20: 9.0,   # cuore
    24: 7.0,   # braccia X
    _NAME_TO_ID.get("shake_hand", 27): 5.5,
}
_ARM_DEFAULT_RELEASE_DELAY = 4.0
_release_gen = 0
_release_gen_lock = threading.Lock()


def _schedule_arm_release(action_id: int, robot_ip: str) -> None:
    """After any arm gesture, auto-release back to neutral after a delay."""
    global _release_gen
    delay = _ARM_RELEASE_DELAYS.get(action_id, _ARM_DEFAULT_RELEASE_DELAY)
    try:
        env_delay = os.getenv("G1_ARM_RELEASE_DELAY_SEC", "").strip()
        if env_delay:
            delay = float(env_delay)
    except ValueError:
        pass
    delay = max(2.0, min(delay, 30.0))

    with _release_gen_lock:
        _release_gen += 1
        token = _release_gen

    def _run():
        time.sleep(delay)
        with _release_gen_lock:
            if token != _release_gen:
                return
        try:
            execute_robot_action("release_arm", robot_ip=robot_ip, _skip_release_schedule=True)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()

def _schedule_shake_hand_release(robot_ip: str) -> None:
    _schedule_arm_release(_SHAKE_HAND_ID, robot_ip)


def _is_shake_hand_action(action_id) -> bool:
    s = str(action_id).strip().lower()
    if s == "shake_hand":
        return True
    try:
        return int(s) == _SHAKE_HAND_ID
    except ValueError:
        return _resolve_action_int(action_id) == _SHAKE_HAND_ID


_ARM_FSM_OK = frozenset({500, 501, 801})
_ARM_FSM_BLOCKED = frozenset({802, 803})


def _parse_fsm_rpc_value(r) -> Optional[int]:
    """Estrae fsm id/mode da risposte SDK con formati diversi tra firmware."""
    if r is None or isinstance(r, bool):
        return None
    if isinstance(r, int):
        return r
    if isinstance(r, (list, tuple)):
        nums: list[int] = []
        for x in r:
            try:
                nums.append(int(x))
            except (TypeError, ValueError):
                continue
        if not nums:
            return None
        for v in nums:
            if v in _ARM_FSM_OK or v in _ARM_FSM_BLOCKED or v >= 100:
                return v
        if len(nums) >= 2 and nums[0] == 0:
            return nums[1]
        if len(nums) == 1:
            return nums[0]
        return nums[-1]
    return None


def _read_loco_fsm(lc) -> tuple[Optional[int], Optional[int]]:
    """Legge FSM id/mode dal LocoClient (API 7001/7002)."""
    fsm_id: Optional[int] = None
    fsm_mode: Optional[int] = None
    try:
        if hasattr(lc, "GetFsmId"):
            r = lc.GetFsmId()
            fsm_id = _parse_fsm_rpc_value(r)
            if fsm_id is None and r is not None:
                print(f"[G1 FSM] GetFsmId raw={r!r}", flush=True)
        if hasattr(lc, "GetFsmMode"):
            r = lc.GetFsmMode()
            fsm_mode = _parse_fsm_rpc_value(r)
    except Exception as ex:
        print(f"[G1 FSM] read failed: {ex}", flush=True)
    return fsm_id, fsm_mode


def _fsm_set_accepted(set_rc: int) -> bool:
    try:
        return int(set_rc) == 0
    except (TypeError, ValueError):
        return False


def _arm_fsm_ready(fsm_id: Optional[int], set_rc: int) -> tuple[bool, str]:
    """True se possiamo provare i gesti braccia (lettura FSM opzionale)."""
    skip = (os.getenv("G1_ARM_SKIP_FSM_CHECK", "") or "").strip().lower() in ("1", "true", "yes")
    if skip:
        return True, "FSM check disabilitato (G1_ARM_SKIP_FSM_CHECK)"
    if fsm_id in _ARM_FSM_OK:
        return True, f"FSM {fsm_id}"
    if fsm_id in _ARM_FSM_BLOCKED:
        return False, (
            f"Robot in FSM {fsm_id} (modalità AI/app). "
            f"Sul telecomando: esci da AI mode, poi L1+A (sport mode)."
        )
    if _fsm_set_accepted(set_rc) and fsm_id is None:
        return True, (
            f"SetFsmId rc={set_rc} (FSM non leggibile da SDK — normale su alcuni firmware). "
            f"Prova il gesto braccio; se non si muove: L1+A sul telecomando."
        )
    if _fsm_set_accepted(set_rc) and fsm_id not in _ARM_FSM_OK:
        return True, (
            f"SetFsmId rc={set_rc}, FSM letto={fsm_id} (non confermato). "
            f"Prova il gesto; se fallisce: L1+A sul telecomando."
        )
    return False, f"FSM {fsm_id}, SetFsmId rc={set_rc} — serve sport mode (L1+A sul telecomando)."


def probe_g1_sport_status(robot_ip: Optional[str] = None) -> dict:
    """
    Diagnostica sport mode: FSM spesso None su G1 — usiamo anche ping locomozione DDS.
    Ritorna dict con status, label, detail, how_to_verify (testo italiano).
    """
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    out: dict = {
        "robot_ip": ip,
        "fsm_id": None,
        "fsm_mode": None,
        "dds_ok": False,
        "loco_vel_rc": None,
        "loco_service": None,
        "sport_status": "unknown",
        "sport_label": "Sconosciuto",
        "arm_gestures_ok": False,
        "detail": "",
        "how_to_verify": (
            "1) Telecomando: esci da AI mode se attiva\n"
            "2) Tieni premuti L1 + A insieme (~2 s) finché il robot è in piedi «attivo»\n"
            "3) Sul PC: premi Ready — se il robot reagisce, DDS OK\n"
            "4) Prova Rilascia braccia poi un gesto\n"
            "5) App Unitree sul telefono: se i gesti funzionano lì, il PC è solo FSM non leggibile"
        ),
    }
    try:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient  # noqa: F401
    except ImportError:
        out["sport_status"] = "no_sdk"
        out["sport_label"] = "SDK assente"
        out["detail"] = "unitree_sdk2py non installato sul Jetson"
        return out

    try:
        with _sdk_lock:
            _ensure_dds_init()
            lc, svc = _ensure_loco_client_locked()
            fsm_id, fsm_mode = _read_loco_fsm(lc)
            vel_rc = lc.SetVelocity(0.0, 0.0, 0.0, 0.5)
        out["fsm_id"] = fsm_id
        out["fsm_mode"] = fsm_mode
        out["loco_vel_rc"] = vel_rc
        out["loco_service"] = svc
        out["dds_ok"] = _fsm_set_accepted(vel_rc)

        if fsm_id in _ARM_FSM_BLOCKED:
            out["sport_status"] = "ai_blocked"
            out["sport_label"] = f"AI mode (FSM {fsm_id})"
            out["arm_gestures_ok"] = False
            out["detail"] = (
                f"Robot in modalità AI/app (FSM {fsm_id}). "
                f"I gesti dal PC sono bloccati. Sul telecomando: esci da AI, poi L1+A."
            )
        elif fsm_id in _ARM_FSM_OK:
            out["sport_status"] = "sport_ok"
            out["sport_label"] = f"Sport OK (FSM {fsm_id})"
            out["arm_gestures_ok"] = True
            out["detail"] = "Sport mode confermata. I gesti braccia dovrebbero funzionare."
        elif fsm_id == 200:
            out["sport_status"] = "ready"
            out["sport_label"] = "Ready (FSM 200)"
            out["arm_gestures_ok"] = False
            out["detail"] = (
                "Robot in Ready (locomozione). Per le braccia serve FSM 500: L1+A sul telecomando "
                "oppure prova un gesto (il Jetson tenta SetFsmId automaticamente)."
            )
        elif out["dds_ok"]:
            out["sport_status"] = "dds_ok"
            out["sport_label"] = "DDS OK — sport non verificabile"
            out["arm_gestures_ok"] = True  # allow try — SetFsmId often works when FSM unreadable
            out["detail"] = (
                "Il Jetson parla col robot (loco rc=0) ma non legge lo FSM (normale su questo firmware). "
                "Non puoi sapere dallo schermo se L1+A è attivo: verifica sul robot (piedi attivi, "
                "stick telecomando muove il robot) oppure premi Ready e guarda se reagisce."
            )
        else:
            out["sport_status"] = "offline"
            out["sport_label"] = "Robot non raggiungibile"
            out["arm_gestures_ok"] = False
            out["detail"] = (
                f"DDS/loco non risponde (SetVelocity rc={vel_rc}). "
                f"Accendi robot, L1+A (sport mode), rete 192.168.123.x, IP {ip}."
            )
    except Exception as e:
        out["sport_status"] = "error"
        out["sport_label"] = "Errore diagnostica"
        out["detail"] = str(e)
    return out


def _ensure_arm_action_fsm() -> tuple[bool, str]:
    """
    I gesti braccia (G1ArmActionClient) richiedono sportmodestate fsm_id in {500,501,801}.
    Ready imposta FSM 200 (locomozione) — insufficiente: i gesti possono risultare «solo braccio dx».
    """
    auto = (os.getenv("G1_ARM_AUTO_FSM", "1") or "1").strip().lower() not in ("0", "false", "no")
    try:
        target = int((os.getenv("G1_ARM_FSM_TARGET", "500") or "500").strip())
    except ValueError:
        target = 500
    try:
        settle = float((os.getenv("G1_ARM_FSM_SETTLE_SEC", "1.2") or "1.2").strip())
    except ValueError:
        settle = 1.2
    settle = max(0.3, min(settle, 5.0))

    with _sdk_lock:
        _ensure_dds_init()
        lc, svc = _ensure_loco_client_locked()
        fsm_id, fsm_mode = _read_loco_fsm(lc)

    if fsm_id in _ARM_FSM_OK:
        if fsm_id == 801 and fsm_mode is not None and fsm_mode not in (0, 3):
            pass
        else:
            extra = f" mode={fsm_mode}" if fsm_mode is not None else ""
            return True, f"FSM {fsm_id}{extra}"

    if not auto:
        return False, (
            f"FSM {fsm_id} non valido per gesti (serve 500/501/801). "
            f"Premi «Modalità gesti» o L1+A sul telecomando."
        )

    with _sdk_lock:
        fsm_before = fsm_id
        rc = lc.SetFsmId(target)
    time.sleep(settle)
    with _sdk_lock:
        fsm_id, fsm_mode = _read_loco_fsm(lc)

    ok, msg = _arm_fsm_ready(fsm_id, rc)
    if ok:
        extra = f" mode={fsm_mode}" if fsm_mode is not None else ""
        if fsm_id in _ARM_FSM_OK:
            return True, f"FSM {fsm_id}{extra} (SetFsmId({target}) rc={rc})"
        return True, f"{msg}{extra}"

    if fsm_id == fsm_before and fsm_before is not None and fsm_before in _ARM_FSM_BLOCKED:
        return False, msg

    if fsm_id == fsm_before and fsm_before is not None:
        return False, (
            f"SetFsmId({target}) rc={rc} ma FSM resta {fsm_before}. "
            f"Il robot ignora i comandi SDK: usa il telecomando (L1+A) o l'app Unitree."
        )

    return False, msg


def _action_result_message(action_id: int, ok: bool, detail: str) -> str:
    meta = G1_ARM_ACTIONS.get(action_id, {})
    label = meta.get("label") or meta.get("name") or str(action_id)
    if ok:
        return f"ok — {label} (id {action_id})"
    return f"{label} (id {action_id}): {detail}"


def execute_robot_action(
    action_id: str,
    robot_ip: Optional[str] = None,
    *,
    _skip_release_schedule: bool = False,
) -> tuple[bool, str]:
    """
    Esegue azione sul robot G1. Ritorna (success, message).
    action_id: nome (shake_hand, high_wave, ...) o int (27, 26, ...).
    Prova: 1) script scripts/robot_action.sh, 2) unitree_sdk2py G1ArmActionClient.
    """
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    shake = _is_shake_hand_action(action_id)

    if SCRIPT_ACTIONS_PATH.exists() and os.access(SCRIPT_ACTIONS_PATH, os.X_OK):
        try:
            r = subprocess.run(
                [str(SCRIPT_ACTIONS_PATH), str(action_id), ip],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=str(SCRIPT_ACTIONS_PATH.parent),
            )
            if r.returncode == 0:
                act_int_resolved = _resolve_action_int(action_id)
                if act_int_resolved is not None and act_int_resolved != 99 and not _skip_release_schedule:
                    _schedule_arm_release(act_int_resolved, ip)
                if act_int_resolved is not None:
                    return True, _action_result_message(act_int_resolved, True, "ok")
                return True, "ok"
            return False, (r.stderr or r.stdout or "script fallito").strip()
        except Exception as e:
            return False, str(e)

    act_int = _resolve_action_int(action_id)
    if act_int is None:
        if str(action_id).startswith("teaching_"):
            return False, f"Teaching: crea scripts/robot_action.sh con logica per {action_id}"
        return False, f"Azione non riconosciuta: {action_id}"

    ok, msg = _do_arm_action(act_int, ip)
    if (
        not ok
        and Path("/sys/class/net/usb0").exists()
        and "channel factory" in (msg or "").lower()
        and (os.getenv("UNITREE_DDS_INTERFACE") or "").strip().lower() != "usb0"
    ):
        print("[G1 DDS] retry arm action with UNITREE_DDS_INTERFACE=usb0", flush=True)
        os.environ["UNITREE_DDS_INTERFACE"] = "usb0"
        _reset_dds_state()
        ok, msg = _do_arm_action(act_int, ip)
    if ok and act_int != 99 and not _skip_release_schedule:
        _schedule_arm_release(act_int, ip)
    if act_int is not None:
        return ok, _action_result_message(act_int, ok, msg)
    return ok, msg


def _do_arm_action(action_id: int, robot_ip: str) -> tuple[bool, str]:
    """Esegue G1 Arm Action via SDK (API 7106 = ExecuteAction)."""
    if action_id != 99:
        fsm_ok, fsm_msg = _ensure_arm_action_fsm()
        print(f"[G1 arm] FSM check: {fsm_msg}", flush=True)
        if not fsm_ok:
            return False, fsm_msg
    if action_id != 99 and action_id in _BOTH_ARMS_ACTION_IDS:
        prep_ok, prep_msg = _do_arm_action_sdk(99, robot_ip)
        if not prep_ok and "7402" not in str(prep_msg):
            print(f"[G1 arm] pre-release 99: {prep_msg}", flush=True)
        else:
            time.sleep(0.35)
    return _do_arm_action_sdk(action_id, robot_ip)


def _do_arm_action_sdk(action_id: int, robot_ip: str) -> tuple[bool, str]:
    """Chiamata SDK ExecuteAction senza pre-release."""
    global _sdk_client
    try:
        from talk_module.arm_sdk import is_arm_sdk_active
        if is_arm_sdk_active():
            return False, "rt/arm_sdk occupato da teaching/VR — attendi o premi STOP"
    except ImportError:
        pass
    try:
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient

        with _arm_sdk_lock:
            _ensure_dds_init()
            if _sdk_client is None:
                _sdk_client = G1ArmActionClient()
                _sdk_client.Init()
                _sdk_client.SetTimeout(10.0)
            client = _sdk_client
        ret = client.ExecuteAction(action_id)
        if ret == 0:
            return True, "ok"
        err_map = {
            7400: "rt/armsdk occupato (teaching/VR attivo — premi STOP)",
            7401: "braccio in hold — riprova dopo Rilascia braccia (99)",
            7402: "action_id non valido per questo firmware",
            7404: "FSM non compatibile: sport mode (L1+A) e stato Ready/Walk",
        }
        hint = ""
        if ret == 7404:
            hint = " Se un solo braccio si muove, verifica anche il braccio SX nell'app Unitree."
        return False, err_map.get(ret, f"errore SDK rc={ret}") + hint
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


def set_led_color(r: int, g: int, b: int) -> tuple[bool, str]:
    """Set G1 forehead LED color via AudioClient.LedControl(R, G, B). Values 0-255."""
    global _audio_client
    try:
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        with _audio_sdk_lock:
            _ensure_dds_init()
            if _audio_client is None:
                _audio_client = AudioClient()
                _audio_client.SetTimeout(5.0)
                _audio_client.Init()
            rc = _audio_client.LedControl(int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF)
        if rc == 0:
            return True, f"LED set to ({r},{g},{b})"
        return False, f"LedControl rc={rc}"
    except ImportError:
        return False, "unitree_sdk2py AudioClient non disponibile"
    except Exception as e:
        return False, f"LED error: {e}"


LED_LISTENING = (0, 120, 255)    # blue: wake word listening
LED_THINKING = (255, 180, 0)     # amber: processing/thinking
LED_SPEAKING = (0, 255, 80)      # green: TTS playing
LED_IDLE = (255, 255, 255)       # white: idle/standby

# --------------- LED animation engine ---------------
_led_anim_stop = threading.Event()
_led_anim_thread: Optional[threading.Thread] = None
_led_anim_lock = threading.Lock()


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (h 0-360, s/v 0-1) to RGB 0-255."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h / 360.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _anim_rainbow(stop_ev: threading.Event, speed: float = 1.0):
    """Smooth rainbow cycle on the LED. speed=1.0 → full cycle in ~4s."""
    hue = 0.0
    step = 3.0 * speed
    while not stop_ev.is_set():
        r, g, b = _hsv_to_rgb(hue % 360, 1.0, 1.0)
        set_led_color(r, g, b)
        hue += step
        stop_ev.wait(0.05)


def _anim_breathe(stop_ev: threading.Event, color: tuple[int, int, int], period: float = 1.5):
    """Breathing/pulse effect: smoothly fades brightness up and down."""
    import math
    t = 0.0
    dt = 0.05
    while not stop_ev.is_set():
        brightness = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(2 * math.pi * t / period))
        r = int(color[0] * brightness)
        g = int(color[1] * brightness)
        b = int(color[2] * brightness)
        set_led_color(r, g, b)
        t += dt
        stop_ev.wait(dt)


def _anim_blink(stop_ev: threading.Event, color: tuple[int, int, int], interval: float = 0.5):
    """Blink on/off at given interval."""
    on = True
    while not stop_ev.is_set():
        if on:
            set_led_color(*color)
        else:
            set_led_color(0, 0, 0)
        on = not on
        stop_ev.wait(interval)


def led_start_animation(mode: str = "rainbow", color: tuple[int, int, int] = (255, 180, 0),
                         speed: float = 1.0) -> None:
    """Start a LED animation in a background thread. Stops any running animation first.
    Modes: 'rainbow', 'breathe', 'blink'."""
    led_stop_animation()
    with _led_anim_lock:
        _led_anim_stop.clear()
        if mode == "rainbow":
            target = _anim_rainbow
            args = (_led_anim_stop, speed)
        elif mode == "breathe":
            target = _anim_breathe
            args = (_led_anim_stop, color, 1.5 / speed)
        elif mode == "blink":
            target = _anim_blink
            args = (_led_anim_stop, color, 0.4 / speed)
        else:
            target = _anim_rainbow
            args = (_led_anim_stop, speed)
        global _led_anim_thread
        _led_anim_thread = threading.Thread(target=target, args=args, daemon=True)
        _led_anim_thread.start()


def led_stop_animation() -> None:
    """Stop any running LED animation."""
    global _led_anim_thread
    with _led_anim_lock:
        _led_anim_stop.set()
        if _led_anim_thread and _led_anim_thread.is_alive():
            _led_anim_thread.join(timeout=1.0)
        _led_anim_thread = None


def _loco_pulse_forward_back(vx: float, n_pulses: int = 2) -> tuple[bool, str]:
    """Due (o n) impulsi di marcia senza tenere _sdk_lock durante sleep."""
    if vx > 0:
        try:
            led_stop_animation()
            set_led_color(255, 0, 0)
        except Exception:
            pass
    parts: list[str] = []
    svc_name = ""
    for i in range(n_pulses):
        with _sdk_lock:
            _ensure_dds_init()
            lc, svc_name = _ensure_loco_client_locked()
            rc = lc.SetVelocity(float(vx), 0.0, 0.0, 0.58)
        parts.append(f"pulse{i + 1} rc={rc}")
        if rc != 0:
            return False, _loco_rpc_message(rc, "loco_pulse") + " | " + "; ".join(parts)
        time.sleep(0.62)
    with _sdk_lock:
        _ensure_dds_init()
        lc, svc_name = _ensure_loco_client_locked()
        rz = lc.SetVelocity(0.0, 0.0, 0.0, 0.35)
    parts.append(f"stop rc={rz}")
    msg = "; ".join(parts) + f" svc={svc_name}"
    _loco_log(f"loco_pulse vx={vx} {msg}")
    return True, msg


def _loco_spin_inplace_macro() -> tuple[bool, str]:
    try:
        steps = max(8, min(80, int((os.getenv("G1_SPIN_STEPS") or "34").strip() or "34")))
    except ValueError:
        steps = 34
    try:
        vyaw = float((os.getenv("G1_SPIN_VYAW") or "0.92").strip() or "0.92")
    except ValueError:
        vyaw = 0.92
    try:
        dt = float((os.getenv("G1_SPIN_DT") or "0.2").strip() or "0.2")
    except ValueError:
        dt = 0.2
    parts: list[str] = []
    svc_name = ""
    for i in range(steps):
        with _sdk_lock:
            _ensure_dds_init()
            lc, svc_name = _ensure_loco_client_locked()
            rc = lc.SetVelocity(0.0, 0.0, vyaw, dt + 0.06)
        if rc != 0:
            return False, _loco_rpc_message(rc, "spin_inplace") + " | " + "; ".join(parts)
        parts.append(f"s{i} rc={rc}")
        time.sleep(dt)
    with _sdk_lock:
        _ensure_dds_init()
        lc, svc_name = _ensure_loco_client_locked()
        rz = lc.SetVelocity(0.0, 0.0, 0.0, 0.35)
    msg = f"spin steps={steps} vyaw={vyaw} stop_rc={rz} svc={svc_name}"
    _loco_log(f"spin_inplace {msg}")
    return True, msg


def _loco_gentle_sway() -> tuple[bool, str]:
    """Leggero dondolio del busto: piccole rotazioni yaw dx/sx stando dritto."""
    cycles = 3
    vyaw = 0.12
    half_period = 0.45
    parts: list[str] = []
    svc_name = ""
    for i in range(cycles):
        direction = vyaw if (i % 2 == 0) else -vyaw
        with _sdk_lock:
            _ensure_dds_init()
            lc, svc_name = _ensure_loco_client_locked()
            rc = lc.SetVelocity(0.0, 0.0, direction, half_period + 0.1)
        if rc != 0:
            return False, _loco_rpc_message(rc, "gentle_sway")
        parts.append(f"sway{i} d={direction:.2f} rc={rc}")
        time.sleep(half_period)
    with _sdk_lock:
        _ensure_dds_init()
        lc, svc_name = _ensure_loco_client_locked()
        rz = lc.SetVelocity(0.0, 0.0, 0.0, 0.35)
    parts.append(f"stop rc={rz}")
    msg = "; ".join(parts) + f" svc={svc_name}"
    _loco_log(f"gentle_sway {msg}")
    return True, msg


def _loco_rpc_message(rc: int, op: str) -> str:
    iface = _dds_interface_for_init()
    return (
        f"{op} rifiutato (codice RPC {rc}, atteso 0). "
        f"Sport mode (es. L1+A), DDS su {iface}. "
        f"Se rc=0 ma non si muove: prova UNITREE_LOCO_SERVICE_NAME=loco. "
        f"G1_READY_SEQUENCE=squat_then_high usa solo Squat2Up (706), senza Damp."
    )


def _run_ready_sequence(lc, svc_name: str) -> tuple[bool, str]:
    iface = _dds_interface_for_init()
    seq = (os.getenv("G1_READY_SEQUENCE") or "standard").strip().lower()
    skip_start = os.getenv("G1_READY_SKIP_START", "").lower() in ("1", "true", "yes")
    UINT32_MAX = float((1 << 32) - 1)
    parts: list[str] = []

    def do_start() -> None:
        if skip_start:
            parts.append("Start skipped")
            return
        # Start() nell'SDK non ritorna il codice RPC; usiamo SetFsmId(200) come fa Start().
        rs = lc.SetFsmId(200)
        parts.append(f"Start(FSM200) rc={rs}")
        time.sleep(0.2)

    if seq in ("squat", "squat_then_high", "from_squat"):
        # Mai Damp qui: FSM 1 = smorzamento passivo → rischio caduta se il robot non è già in squat.
        ru = lc.SetFsmId(706)
        parts.append(f"Squat2Up rc={ru}")
        if ru != 0:
            return False, _loco_rpc_message(ru, "Squat2StandUp (706)") + " | " + "; ".join(parts)
        time.sleep(1.0)
        do_start()
    else:
        do_start()

    rh = lc.SetStandHeight(UINT32_MAX)
    parts.append(f"StandH rc={rh}")
    if rh != 0:
        return False, _loco_rpc_message(rh, "Ready (HighStand)") + " | " + "; ".join(parts)
    msg = "; ".join(parts) + f". iface={iface} svc={svc_name} seq={seq}"
    _loco_log(f"ready OK {msg}")
    return True, msg


def send_move_command(vx: float, vy: float, vyaw: float, robot_ip: Optional[str] = None) -> tuple[bool, str]:
    """G1: velocità via LocoClient SetVelocity (vx avanti, vy laterale, vyaw rad/s)."""
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    try:
        with _sdk_lock:
            _ensure_dds_init()
            lc, svc = _ensure_loco_client_locked()
            rc = lc.SetVelocity(float(vx), float(vy), float(vyaw), 1.0)
            iface = _dds_interface_for_init()
            msg = f"Move rc={rc} iface={iface} svc={svc}"
            _loco_log(msg)
            if rc != 0:
                return False, _loco_rpc_message(rc, "Move/SetVelocity") + f" | {msg}"
        return True, msg
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


def execute_g1_loco_command(
    command: str,
    robot_ip: Optional[str] = None,
    *,
    confirmed: bool = False,
    mode: Optional[int] = None,
) -> tuple[bool, str]:
    """
    Locomozione G1 (unitree_sdk2 C++ / DeepWiki: balance 0=stand statico, 1=gait continuo).
    Comandi: ready, squat_up (706), squat_down (FSM 2), stand_up_simple (FSM 4), sit (3),
    locked_standing / continuous_gait, set_balance + mode, walk_slow / walk_fast, …
    damp / zero_torque / squat_up_damp / lie_standup richiedono confirmed=True.
    """
    ip = robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    cmd = (command or "").strip().lower()

    if loco_command_requires_confirm(cmd) and not confirmed:
        return (
            False,
            "Comando PERICOLOSO bloccato: serve conferma nel client («Sei sicuro?») che invia confirmed=true, "
            "oppure conferma esplicita via API. Evita cadute e smorzamento involontario.",
        )

    def _run_loco_script() -> Optional[tuple[bool, str]]:
        if not (SCRIPT_ACTIONS_PATH.exists() and os.access(SCRIPT_ACTIONS_PATH, os.X_OK)):
            return None
        try:
            r = subprocess.run(
                [str(SCRIPT_ACTIONS_PATH), f"loco_{cmd}", ip],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=str(SCRIPT_ACTIONS_PATH.parent),
            )
            if r.returncode == 0:
                msg = (r.stdout or "").strip() or "ok"
                return True, msg[:500]
            err = (r.stderr or r.stdout or "script fallito").strip()
            return False, err[:500]
        except Exception as e:
            return False, str(e)

    try:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient  # noqa: F401
    except ImportError:
        out = _run_loco_script()
        if out is not None:
            return out
        return False, "Installa unitree_sdk2_python con modulo G1 LocoClient"

    script_first = os.getenv("G1_LOCO_SCRIPT_FIRST", "").lower() in ("1", "true", "yes")
    if script_first:
        out = _run_loco_script()
        if out is not None and out[0]:
            return out

    UINT32_MAX = float((1 << 32) - 1)
    _macro_fwd = frozenset(
        {"double_step_forward", "due_passi_avanti", "two_steps_forward", "due passi avanti"}
    )
    _macro_back = frozenset(
        {"double_step_back", "due_passi_indietro", "due passi indietro"}
    )
    _macro_spin = frozenset({"spin_inplace", "gira_su_te_stesso", "ruotati", "gira"})
    _macro_sway = frozenset({"gentle_sway", "dondola", "dondolio", "ondeggia"})
    _macro_walk_long = frozenset(
        {"walk_forward_10", "dieci_passi_avanti", "cammina_avanti"}
    )
    if cmd in _macro_fwd:
        return _loco_pulse_forward_back(0.22, 2)
    if cmd in _macro_back:
        return _loco_pulse_forward_back(-0.2, 2)
    if cmd in _macro_spin:
        return _loco_spin_inplace_macro()
    if cmd in _macro_sway:
        return _loco_gentle_sway()
    if cmd in _macro_walk_long:
        return _loco_pulse_forward_back(0.35, 10)

    try:
        with _sdk_lock:
            _ensure_dds_init()
            lc, svc_name = _ensure_loco_client_locked()
            if cmd in ("ready", "pronto", "high_stand"):
                ok, msg = _run_ready_sequence(lc, svc_name)
                return ok, msg
            if cmd in ("arm_ready", "gestures", "arm_mode", "fsm_gestures", "modalita_gesti"):
                rc = lc.SetFsmId(500)
                time.sleep(1.0)
                fsm_id, fsm_mode = _read_loco_fsm(lc)
                extra = f" mode={fsm_mode}" if fsm_mode is not None else ""
                msg = f"Modalità gesti FSM500 rc={rc} now={fsm_id}{extra} svc={svc_name}"
                _loco_log(msg)
                if not _fsm_set_accepted(rc):
                    return False, _loco_rpc_message(rc, "SetFsmId(500)") + f" | {msg}"
                ok, hint = _arm_fsm_ready(fsm_id, rc)
                if ok:
                    return True, f"{msg} | {hint}"
                return False, f"{hint} | {msg}"
            if cmd in ("squat_up", "alzati", "squat2stand", "squat2standup"):
                # Solo transizione SDK «da squat» (706). NON Damp: il Damp (1) manda in collasso se non sei in squat.
                ru = lc.SetFsmId(706)
                parts = [f"Squat2Up rc={ru}"]
                if ru != 0:
                    return False, _loco_rpc_message(ru, "Squat2StandUp") + " | " + "; ".join(parts)
                iface = _dds_interface_for_init()
                msg = "; ".join(parts) + f". iface={iface} svc={svc_name} (no Damp)"
                _loco_log(f"squat_up {msg}")
                return True, msg
            if cmd in ("squat_down", "stand_to_squat", "squat_fsm", "accosciati"):
                # unitree_sdk2 C++: Squat() = FSM 2 (da stance in piedi verso squat). Diverso da 706 (Squat2StandUp).
                rc = lc.SetFsmId(2)
                msg = f"Squat FSM2 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "Squat FSM2") + f" | {msg}"
                return True, msg
            if cmd in ("stand_up_simple", "standup_fsm", "stand_basic", "alzati_semplice"):
                rc = lc.SetFsmId(4)
                msg = f"StandUp FSM4 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "StandUp FSM4") + f" | {msg}"
                return True, msg
            if cmd in ("sit", "siediti"):
                rc = lc.SetFsmId(3)
                msg = f"Sit FSM3 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "Sit") + f" | {msg}"
                return True, msg
            if cmd in ("loco_start", "start_locomotion"):
                rc = lc.SetFsmId(200)
                msg = f"LocoStart FSM200 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "LocoStart") + f" | {msg}"
                return True, msg
            if cmd in ("locked_standing", "balance_static", "regular_mode", "stand_balance"):
                rc = lc.SetBalanceMode(0)
                msg = f"BalanceMode(0 stand statico / «locked») rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "SetBalanceMode(0)") + f" | {msg}"
                return True, msg
            if cmd in ("continuous_gait", "gait_mode", "running_mode", "gait"):
                rc = lc.SetBalanceMode(1)
                msg = f"BalanceMode(1 gait continuo / «running») rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "SetBalanceMode(1)") + f" | {msg}"
                return True, msg
            if cmd in ("set_balance", "balance_mode_set"):
                if mode is None:
                    return (
                        False,
                        "Per set_balance serve nel JSON il campo mode: 0 = stand statico, 1 = gait continuo (doc. Unitree G1).",
                    )
                if mode not in (0, 1):
                    return False, "set_balance: mode consentiti solo 0 oppure 1."
                rc = lc.SetBalanceMode(mode)
                msg = f"BalanceMode({mode}) rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, f"SetBalanceMode({mode})") + f" | {msg}"
                return True, msg
            if cmd == "squat_up_damp":
                # Come esempio Unitree: Damp poi 706 — solo dopo conferma esplicita.
                parts = []
                rd = lc.SetFsmId(1)
                parts.append(f"Damp rc={rd}")
                time.sleep(0.5)
                ru = lc.SetFsmId(706)
                parts.append(f"Squat2Up rc={ru}")
                if ru != 0:
                    return False, _loco_rpc_message(ru, "Squat2StandUp") + " | " + "; ".join(parts)
                iface = _dds_interface_for_init()
                msg = "; ".join(parts) + f". iface={iface} svc={svc_name}"
                _loco_log(f"squat_up_damp {msg}")
                return True, msg
            if cmd == "damp":
                rc = lc.SetFsmId(1)
                msg = f"Damp rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "Damp") + f" | {msg}"
                return True, msg
            if cmd in ("zero_torque", "zero_coppia"):
                rc = lc.SetFsmId(0)
                msg = f"ZeroTorque rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "ZeroTorque") + f" | {msg}"
                return True, msg
            if cmd in ("lie_standup", "lie2stand"):
                rc = lc.SetFsmId(702)
                msg = f"Lie2StandUp rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "Lie2StandUp") + f" | {msg}"
                return True, msg
            if cmd in ("low_stand", "basso"):
                rc = lc.SetStandHeight(0.0)
                msg = f"StandH(low) rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "LowStand") + f" | {msg}"
                return True, msg
            if cmd in ("walk_slow", "cammina_lento", "passo_lento"):
                rc = lc.SetVelocity(0.15, 0.0, 0.0, 1.0)
                msg = f"WalkSlow vx=0.15 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "WalkSlow") + f" | {msg}"
                return True, msg
            if cmd in ("walk_fast", "cammina_veloce", "passo_veloce", "run_step"):
                rc = lc.SetVelocity(0.45, 0.0, 0.0, 1.0)
                msg = f"WalkFast vx=0.45 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "WalkFast") + f" | {msg}"
                return True, msg
            if cmd in ("walk", "cammina", "avanti"):
                rc = lc.SetVelocity(0.28, 0.0, 0.0, 1.0)
                msg = f"Walk vx=0.28 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "Walk") + f" | {msg}"
                return True, msg
            if cmd in ("stop_walk", "stop", "ferma", "stand_still", "fermo_equilibrio"):
                rc = lc.SetVelocity(0.0, 0.0, 0.0, 1.0)
                msg = f"Vel0 rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "Stop/Vel0") + f" | {msg}"
                return True, msg
            if cmd in ("balance_stand", "bilanciamento"):
                try:
                    bm = mode if mode is not None else int((os.getenv("G1_BALANCE_MODE") or "0").strip() or "0")
                except ValueError:
                    bm = 0
                rc = lc.SetBalanceMode(bm)
                msg = f"BalanceMode({bm}) rc={rc} svc={svc_name}"
                _loco_log(msg)
                if rc != 0:
                    return False, _loco_rpc_message(rc, "SetBalanceMode") + f" | {msg}"
                return True, msg
            return False, f"Comando locomozione sconosciuto: {command}"
    except Exception as e:
        return False, str(e)
