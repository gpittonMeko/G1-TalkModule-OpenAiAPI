"""HTTP API for Unitree Explore app teachings (record on phone, play from web)."""

from __future__ import annotations

import os

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from talk_module.explore_teaching import (
    explore_teaching_status,
    list_explore_teachings,
    play_explore_teaching,
    stop_explore_teaching,
)

router = APIRouter(prefix="/api/explore-teachings", tags=["explore-teachings"])


class PlayBody(BaseModel):
    name: str = ""
    action_name: str = ""
    robot_ip: str | None = None


class StopBody(BaseModel):
    robot_ip: str | None = None


@router.get("")
@router.get("/")
def explore_teachings_list():
    return list_explore_teachings()


@router.get("/status")
def explore_teachings_status():
    robot_ip = os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    return explore_teaching_status(robot_ip=robot_ip)


@router.post("/play")
def explore_teachings_play(body: PlayBody):
    name = (body.name or body.action_name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "name richiesto"}, status_code=400)
    robot_ip = body.robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    result = play_explore_teaching(name, robot_ip=robot_ip)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/stop")
def explore_teachings_stop(body: StopBody = Body(default=StopBody())):
    robot_ip = body.robot_ip or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
    result = stop_explore_teaching(robot_ip=robot_ip)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)
