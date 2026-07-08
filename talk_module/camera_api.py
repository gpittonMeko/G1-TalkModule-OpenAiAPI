"""API HTTP: stream MJPEG camera + YOLO per dashboard."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response, StreamingResponse

from talk_module.camera_yolo import get_camera_service

router = APIRouter(prefix="/api/camera", tags=["camera"])


@router.get("/status")
def camera_status():
    svc = get_camera_service()
    return {"ok": True, **svc.status()}


@router.post("/start")
def camera_start():
    svc = get_camera_service()
    svc.start()
    return {"ok": True, **svc.status()}


@router.post("/stop")
def camera_stop():
    svc = get_camera_service()
    svc.stop()
    return {"ok": True, **svc.status()}


@router.get("/snapshot")
def camera_snapshot():
    svc = get_camera_service()
    if not svc.status().get("running"):
        svc.start()
        for _ in range(40):
            jpeg = svc.get_jpeg()
            if jpeg:
                return Response(content=jpeg, media_type="image/jpeg")
            time.sleep(0.05)
    jpeg = svc.get_jpeg()
    if not jpeg:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "message": "Nessun frame camera. Verifica G1_CAMERA_DEVICE o RealSense."},
        )
    return Response(content=jpeg, media_type="image/jpeg")


async def _mjpeg_generator():
    svc = get_camera_service()
    svc.start()
    boundary = b"--frame"
    while True:
        jpeg = svc.get_jpeg()
        if jpeg:
            yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        await asyncio.sleep(1.0 / max(svc.fps, 5))


@router.get("/stream")
async def camera_stream():
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )
