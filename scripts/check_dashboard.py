#!/usr/bin/env python3
import json
import ssl
import urllib.request

ctx = ssl._create_unverified_context()
base = "https://127.0.0.1:8081"

def get(path):
    with urllib.request.urlopen(base + path, context=ctx, timeout=10) as r:
        return r.status, r.read()

checks = []
for path in ["/api/health", "/client", "/api/soundboard?lite=1", "/api/robot-unitree-teachings"]:
    try:
        code, body = get(path)
        info = {"path": path, "status": code, "bytes": len(body)}
        if path.startswith("/api/soundboard"):
            d = json.loads(body)
            info["slots"] = len(d.get("slots", []))
            info["slot_count"] = d.get("slot_count")
        if "unitree" in path:
            d = json.loads(body)
            info["custom"] = len(d.get("custom", []))
            info["ok"] = d.get("ok")
        if path == "/client":
            html = body.decode("utf-8", errors="replace")
            info["has_soundboard"] = "soundboardGrid" in html
            info["has_robot_tab"] = "g1RobotPanelTab" in html
        checks.append(info)
    except Exception as e:
        checks.append({"path": path, "error": str(e)})

print(json.dumps(checks, indent=2))
