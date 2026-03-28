"""
FastAPI router for Teaching endpoints.

Endpoints:
  POST /api/teaching/start_record
  POST /api/teaching/stop_record
  POST /api/teaching/replay_temp
  POST /api/teaching/replay_slot/{slot_id}
  POST /api/teaching/save_to_slot/{slot_id}
  POST /api/teaching/emergency_stop
  POST /api/teaching/delete/{slot_id}
  GET  /api/teaching/status
  GET  /api/teaching/list
  GET  /api/teaching/stream   (SSE at ~10Hz)
"""

import asyncio
import json
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from talk_module.teaching import TeachingManager, TeachingState
from talk_module import teaching_store

router = APIRouter(prefix="/api/teaching", tags=["teaching"])

_manager = TeachingManager()


@router.post("/start_record")
async def start_record():
    result = _manager.start_recording()
    return JSONResponse(result, status_code=200 if result["ok"] else 409)


@router.post("/stop_record")
async def stop_record():
    result = _manager.stop_recording()
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@router.post("/replay_temp")
async def replay_temp():
    result = _manager.replay_temp()
    return JSONResponse(result, status_code=200 if result["ok"] else 409)


@router.post("/replay_slot/{slot_id}")
async def replay_slot(slot_id: int):
    result = _manager.replay_slot(slot_id)
    return JSONResponse(result, status_code=200 if result["ok"] else 409)


@router.post("/save_to_slot/{slot_id}")
async def save_to_slot(slot_id: int):
    result = _manager.save_to_slot(slot_id)
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@router.post("/emergency_stop")
async def emergency_stop():
    result = _manager.emergency_stop()
    return JSONResponse(result)


@router.post("/delete/{slot_id}")
async def delete_teaching(slot_id: int):
    deleted = teaching_store.delete_trajectory(slot_id)
    return JSONResponse({"ok": deleted})


@router.get("/status")
async def status():
    return JSONResponse(_manager.get_status())


@router.get("/list")
async def list_teachings():
    return JSONResponse(teaching_store.list_teachings())


@router.get("/stream")
async def stream_joints():
    """SSE endpoint that pushes joint state at ~10Hz while active."""

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
