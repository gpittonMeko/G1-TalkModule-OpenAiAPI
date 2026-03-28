"""
FastAPI router for VR teleoperation endpoints.

Endpoints:
  POST /api/vr/start           -- init arm SDK + UDP receiver
  POST /api/vr/stop            -- ramp down, stop
  POST /api/vr/calibrate       -- snapshot neutral hand pose
  POST /api/vr/locomotion      -- {vx, vy, vyaw} from Quest gamepad
  POST /api/vr/emergency_stop  -- immediate stop
  GET  /api/vr/status          -- current state + tracking info
  GET  /api/vr/stream          -- SSE at ~10Hz
"""

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from talk_module.vr_teleop import VRTeleopManager

router = APIRouter(prefix="/api/vr", tags=["vr"])

_manager = VRTeleopManager()


class LocoInput(BaseModel):
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0


@router.post("/start")
async def vr_start():
    result = _manager.start()
    return JSONResponse(result, status_code=200 if result["ok"] else 409)


@router.post("/stop")
async def vr_stop():
    result = _manager.stop()
    return JSONResponse(result)


@router.post("/calibrate")
async def vr_calibrate():
    result = _manager.calibrate()
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@router.post("/locomotion")
async def vr_locomotion(data: LocoInput):
    try:
        from talk_module.robot_actions import send_move_command
        ok, msg = send_move_command(data.vx, data.vy, data.vyaw)
        return JSONResponse({"ok": ok, "message": msg})
    except ImportError:
        return JSONResponse({"ok": False, "error": "locomotion not available"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/emergency_stop")
async def vr_emergency_stop():
    result = _manager.emergency_stop()
    return JSONResponse(result)


@router.get("/status")
async def vr_status():
    return JSONResponse(_manager.get_status())


@router.get("/stream")
async def vr_stream():
    """SSE endpoint pushing VR state at ~10Hz."""

    async def event_generator():
        while True:
            s = _manager.get_status()
            payload = json.dumps(s, separators=(",", ":"))
            yield f"data: {payload}\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
