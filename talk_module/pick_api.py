"""API HTTP: auto-pick su detection YOLO + depth."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from talk_module.pick_on_detect import get_pick_service

router = APIRouter(prefix="/api/pick", tags=["pick"])


class PickEnableBody(BaseModel):
    enabled: bool


@router.get("/status")
def pick_status():
    return {"ok": True, **get_pick_service().status()}


@router.post("/enable")
def pick_enable(body: PickEnableBody):
    svc = get_pick_service()
    svc.set_enabled(body.enabled)
    return {"ok": True, **svc.status()}


@router.post("/trigger")
def pick_trigger_manual():
    """Test: pick manuale (manovra o teaching secondo G1_PICK_MODE)."""
    result = get_pick_service().trigger_manual()
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/calibrate")
def pick_calibrate():
    """Salva posizione attuale della bottiglia (bbox + depth) come riferimento."""
    result = get_pick_service().calibrate_from_camera()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/maneuver")
def pick_maneuver_test():
    """Test manovra safe_reach (senza aspettare auto-pick)."""
    from talk_module.pick_maneuver import start_safe_reach

    svc = get_pick_service()
    st = svc.status()
    det = st.get("last_detection")
    adj = None
    if det and st.get("ref_bbox_u") is not None and st.get("ref_depth_m") is not None:
        adj = svc._compute_adj(det, st["ref_bbox_u"], st["ref_depth_m"])
    result = start_safe_reach(adj)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)
