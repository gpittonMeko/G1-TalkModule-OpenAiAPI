"""H2 arm/hand control API for dashboard (Thor lab; graceful degrade on Windows)."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from talk_module.diagnostics_log import diag_log, get_lines

router = APIRouter(prefix="/api/h2", tags=["h2"])

H2_DEMO = Path(os.environ.get("H2_DEMO_ROOT", "/home/unitree/h2_demo"))
H2_IFACE = os.environ.get("H2_DDS_IFACE", os.environ.get("UNITREE_DDS_INTERFACE", "eth10"))
ENV_PREFIX = (
    "export CYCLONEDDS_HOME=/usr/local "
    "LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH "
    f"H2_DDS_IFACE={H2_IFACE} "
    f"UNITREE_DDS_INTERFACE={H2_IFACE}"
)

_arm_lock = threading.Lock()
_arm_proc: Optional[subprocess.Popen] = None
_arm_log: list[str] = []


def _is_h2_lab() -> bool:
    if sys.platform == "win32":
        return False
    return H2_DEMO.is_dir() and (H2_DEMO / "scripts" / "h2_left_arm_raise.py").is_file()


def _h2_unavailable_detail() -> str:
    if sys.platform == "win32":
        return (
            "Server Windows locale: movimenti H2 solo su Jetson Thor. "
            "In Impostazioni imposta IP 192.168.123.163 oppure testa layout/log/TTS in locale."
        )
    return f"h2_demo non trovata in {H2_DEMO}. Esegui deploy da unitree-h2-testing."


def _run_script(name: str, args: list[str], timeout: int = 120) -> dict[str, Any]:
    script = H2_DEMO / "scripts" / name
    if not script.is_file():
        raise HTTPException(status_code=503, detail=f"Script mancante: {script}")
    cmd = f"cd {H2_DEMO} && {ENV_PREFIX} && python3 scripts/{name} " + " ".join(a for a in args if a)
    diag_log("movement", f"exec {name}")
    try:
        r = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        out = (r.stdout or "") + (r.stderr or "")
        for ln in out.strip().splitlines()[-15:]:
            diag_log("movement", ln)
        if r.returncode != 0:
            diag_log("movement", f"FAIL exit={r.returncode}")
            return {"ok": False, "exit_code": r.returncode, "output": out[-4000:]}
        diag_log("movement", "OK exit=0")
        return {"ok": True, "exit_code": 0, "output": out[-4000:]}
    except subprocess.TimeoutExpired:
        diag_log("movement", f"FAIL timeout after {timeout}s")
        raise HTTPException(status_code=504, detail=f"Timeout dopo {timeout}s")


class ArmMoveBody(BaseModel):
    rise: float = Field(8.0, ge=1.0, le=30.0)
    hold: float = Field(2.0, ge=0.0, le=30.0)
    lower: float = Field(8.0, ge=1.0, le=30.0)


class HandGripBody(BaseModel):
    side: str = Field("left")
    close_fraction: float = Field(0.5, ge=0.0, le=0.98)
    open_hand: bool = False


@router.get("/status")
def h2_status():
    platform_id = "h2" if _is_h2_lab() else ("windows" if sys.platform == "win32" else "g1")
    talk_adapter = Path(os.environ.get("G1_TALK_ROOT", "/home/unitree/G1-TalkModule-OpenAiAPI"))
    adapter_sh = talk_adapter / "scripts" / "robot_action.sh"
    return {
        "ok": True,
        "platform": platform_id,
        "h2_lab_available": _is_h2_lab(),
        "h2_demo_path": str(H2_DEMO),
        "dds_iface": H2_IFACE,
        "adapter_present": adapter_sh.is_file(),
        "hostname": platform.node(),
        "detail": None if _is_h2_lab() else _h2_unavailable_detail(),
    }


@router.get("/arm/status")
def arm_status():
    global _arm_proc
    running = _arm_proc is not None and _arm_proc.poll() is None
    exit_code = None if running else (_arm_proc.poll() if _arm_proc else None)
    return {
        "ok": True,
        "running": running,
        "exit_code": exit_code,
        "log": _arm_log[-40:],
    }


@router.post("/arm/move")
def arm_move(body: ArmMoveBody = Body(...)):
    if not _is_h2_lab():
        raise HTTPException(status_code=503, detail=_h2_unavailable_detail())

    def _worker():
        global _arm_proc, _arm_log
        with _arm_lock:
            _arm_log = []
            args = [
                f"--rise {body.rise}",
                f"--hold {body.hold}",
                f"--lower {body.lower}",
                "--yes",
            ]
            if H2_IFACE:
                args.append(f"--iface {H2_IFACE}")
            cmd = [
                "bash",
                "-lc",
                f"cd {H2_DEMO} && {ENV_PREFIX} && python3 scripts/h2_left_arm_raise.py " + " ".join(args),
            ]
            diag_log("movement", f"arm move rise={body.rise} hold={body.hold} lower={body.lower}")
            try:
                _arm_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                )
                assert _arm_proc.stdout is not None
                for ln in _arm_proc.stdout:
                    ln = ln.rstrip()
                    if ln:
                        _arm_log.append(ln)
                        diag_log("movement", ln)
                _arm_proc.wait(timeout=300)
                diag_log("movement", f"arm finished exit={_arm_proc.returncode}")
            except Exception as e:
                diag_log("movement", f"FAIL {e}")
                _arm_log.append(str(e))

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "started": True, "message": "Movimento braccio avviato"}


@router.post("/arm/stop")
def arm_stop():
    if not _is_h2_lab():
        raise HTTPException(status_code=503, detail=_h2_unavailable_detail())
    iface_args = [f"--iface {H2_IFACE}"] if H2_IFACE else []
    return _run_script("h2_arm_emergency_stop.py", iface_args, timeout=30)


@router.post("/hand/probe")
def hand_probe():
    if not _is_h2_lab():
        raise HTTPException(status_code=503, detail=_h2_unavailable_detail())
    r = _run_script("h2_probe_hand_dds.py", [f"--iface {H2_IFACE}", "--timeout 12"], timeout=45)
    if not r.get("ok"):
        diag_log("hand", "FAIL hand-dds — avvia servizio mani su PC2 (.162)")
    else:
        diag_log("hand", "OK hand-dds")
    return r


@router.post("/hand/wake-pc2")
def hand_wake_pc2():
    if not _is_h2_lab():
        raise HTTPException(status_code=503, detail=_h2_unavailable_detail())
    diag_log("hand", "wake-pc2 start")
    try:
        import paramiko
    except ImportError:
        diag_log("hand", "FAIL paramiko mancante — pip install paramiko")
        raise HTTPException(
            status_code=503,
            detail="paramiko mancante. pip install paramiko oppure h2_start_brainco_pc2.py dal PC lab.",
        )

    pc2 = os.environ.get("H2_PC2_HOST", "192.168.123.162")
    pc2_pw = os.environ.get("H2_PC2_PASSWORD", "Unitree#24226")
    bin_path = "/home/unitree/brainco_hand_service/bin/brainco_hand_server"
    script = f"""
set +e
PW='{pc2_pw}'
BIN='{bin_path}'
IFACE='eth0'
echo "$PW" | sudo -S pkill -f brainco_hand_server 2>/dev/null || true
sleep 1
echo "$PW" | sudo -S nohup "$BIN" -n "$IFACE" > /tmp/brainco_hand_server.log 2>&1 &
sleep 3
pgrep -f brainco_hand_server && echo START_OK || echo START_FAIL
"""
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(pc2, username="unitree", password=pc2_pw, timeout=20, allow_agent=False, look_for_keys=False)
        _, stdout, stderr = c.exec_command("bash -s", timeout=45)
        stdout.channel.send(script)
        stdout.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        c.close()
        for ln in out.splitlines():
            diag_log("hand", ln)
        ok = "START_OK" in out
        if not ok:
            diag_log("hand", f"FAIL wake PC2 {pc2}")
            return {"ok": False, "output": out, "stderr": err}
        diag_log("hand", f"wake-pc2 OK on {pc2}")
        probe = hand_probe()
        return {"ok": True, "pc2": pc2, "probe": probe}
    except Exception as e:
        diag_log("hand", f"FAIL SSH PC2: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/hand/grip")
def hand_grip(body: HandGripBody = Body(...)):
    if not _is_h2_lab():
        raise HTTPException(status_code=503, detail=_h2_unavailable_detail())
    frac = 0.0 if body.open_hand else body.close_fraction
    side = body.side if body.side in ("left", "right") else "left"
    py = (
        f"import sys; sys.path.insert(0,{str(H2_DEMO / 'scripts')!r}); "
        f"from h2_hand_util import run_gentle_grip; "
        f"run_gentle_grip(side={side!r}, close_fraction={frac}, hold_s=1.0, open_after=True); "
        f"print('GRIP_OK')"
    )
    cmd = f"cd {H2_DEMO} && {ENV_PREFIX} && python3 -c {repr(py)}"
    diag_log("hand", f"grip side={side} fraction={frac}")
    try:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=60, errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        for ln in out.splitlines()[-10:]:
            diag_log("hand", ln)
        if r.returncode != 0 or "GRIP_OK" not in out:
            diag_log("hand", "FAIL grip — verifica wake PC2 e hand-dds")
            return {"ok": False, "output": out}
        return {"ok": True, "output": out}
    except Exception as e:
        diag_log("hand", f"FAIL {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/{channel}")
def h2_channel_logs(channel: str, lines: int = 60):
    from talk_module.diagnostics_log import filter_file_lines
    from talk_module.web_app import _tail_server_log

    ch = channel if channel in ("opencv", "movement", "hand", "tts", "general") else "general"
    mem = get_lines(ch, lines)  # type: ignore[arg-type]
    file_rows = filter_file_lines(_tail_server_log(max(lines, 80)), ch)  # type: ignore[arg-type]
    merged = file_rows + [x for x in mem if x not in file_rows]
    return {"ok": True, "channel": ch, "lines": merged[-lines:]}
