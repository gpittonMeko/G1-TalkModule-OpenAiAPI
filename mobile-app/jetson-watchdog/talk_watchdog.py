#!/usr/bin/env python3
"""
talk_watchdog.py  –  Lightweight companion HTTP service for G1 Talk Remote app.

Runs on the Jetson alongside the main talk service, on port 8082.
Provides endpoints to check status, start/stop/restart the talk service,
and tail its log.  Zero external dependencies (stdlib only).

Security: set WATCHDOG_TOKEN env var; the app sends
  Authorization: Bearer <token>
Omit the env var to disable auth (local-only use).

Usage:
  python3 talk_watchdog.py [--port 8082] [--project-root /home/unitree/G1-TalkModule-OpenAiAPI]
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

TALK_PROCESS_PATTERN = "talk_module.web_app"
LOG_PATH = "/tmp/talk.log"
DEFAULT_PORT = 8082
DEFAULT_PROJECT_ROOT = None  # auto-detect


def _find_project_root():
    """Walk up from this script to find the repo root (has scripts/restart_server.sh)."""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if os.path.isfile(os.path.join(d, "scripts", "restart_server.sh")):
            return d
        d = os.path.dirname(d)
    return "/home/unitree/G1-TalkModule-OpenAiAPI"


class WatchdogHandler(BaseHTTPRequestHandler):

    project_root = None
    token = None

    def log_message(self, fmt, *args):
        pass  # silence default stderr logging

    # ── Auth ─────────────────────────────────

    def _check_auth(self):
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {self.token}":
            return True
        self._respond(403, {"error": "forbidden"})
        return False

    # ── Routing ──────────────────────────────

    def do_GET(self):
        if not self._check_auth():
            return
        path = self.path.split("?")[0].rstrip("/")
        routes = {
            "/health":      self._health,
            "/talk-status": self._talk_status,
            "/talk-log":    self._talk_log,
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_auth():
            return
        path = self.path.split("?")[0].rstrip("/")
        routes = {
            "/talk-restart": self._talk_restart,
            "/talk-stop":    self._talk_stop,
            "/talk-start":   self._talk_start,
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._respond(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── Handlers ─────────────────────────────

    def _health(self):
        self._respond(200, {"status": "ok", "service": "talk-watchdog"})

    def _talk_status(self):
        running = _is_talk_running()
        self._respond(200, {
            "running": running,
            "status": "running" if running else "stopped",
        })

    def _talk_log(self):
        lines = _tail_log(50)
        self._respond(200, {"lines": lines})

    def _talk_restart(self):
        ok, msg = _run_restart_script(self.project_root)
        code = 200 if ok else 500
        self._respond(code, {"ok": ok, "message": msg})

    def _talk_stop(self):
        ok, msg = _stop_talk()
        code = 200 if ok else 500
        self._respond(code, {"ok": ok, "message": msg})

    def _talk_start(self):
        ok, msg = _run_restart_script(self.project_root)
        code = 200 if ok else 500
        self._respond(code, {"ok": ok, "message": msg})

    # ── Response Helpers ─────────────────────

    def _respond(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")


# ── Service management helpers ───────────────

def _is_talk_running():
    try:
        r = subprocess.run(
            ["pgrep", "-f", TALK_PROCESS_PATTERN],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _stop_talk():
    try:
        subprocess.run(
            ["pkill", "-f", TALK_PROCESS_PATTERN],
            capture_output=True, timeout=10,
        )
        time.sleep(1)
        if _is_talk_running():
            subprocess.run(
                ["pkill", "-9", "-f", TALK_PROCESS_PATTERN],
                capture_output=True, timeout=5,
            )
            time.sleep(1)
        running = _is_talk_running()
        if running:
            return False, "Processo ancora attivo dopo kill -9"
        return True, "Servizio arrestato"
    except Exception as e:
        return False, str(e)


def _run_restart_script(project_root):
    script = os.path.join(project_root, "scripts", "restart_server.sh")
    if not os.path.isfile(script):
        return False, f"Script non trovato: {script}"
    try:
        r = subprocess.run(
            ["bash", script],
            capture_output=True, text=True, timeout=90,
            cwd=project_root,
        )
        stdout = r.stdout.strip()
        if r.returncode == 0 and "OK:" in stdout:
            return True, "Servizio avviato con successo"
        return False, f"exit={r.returncode}: {stdout[-200:]}"
    except subprocess.TimeoutExpired:
        if _is_talk_running():
            return True, "Avviato (timeout script ma processo attivo)"
        return False, "Timeout esecuzione script"
    except Exception as e:
        return False, str(e)


def _tail_log(n=50):
    try:
        if not os.path.isfile(LOG_PATH):
            return ["(log file non presente)"]
        with open(LOG_PATH, "r", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except Exception as e:
        return [f"Errore lettura log: {e}"]


# ── Main ─────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="G1 Talk Watchdog")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-root", default=None)
    args = parser.parse_args()

    project_root = args.project_root or _find_project_root()
    token = os.environ.get("WATCHDOG_TOKEN", "")

    WatchdogHandler.project_root = project_root
    WatchdogHandler.token = token

    server = HTTPServer(("0.0.0.0", args.port), WatchdogHandler)
    print(f"[watchdog] listening on 0.0.0.0:{args.port}  root={project_root}")
    if token:
        print(f"[watchdog] auth enabled (WATCHDOG_TOKEN set)")
    else:
        print(f"[watchdog] auth disabled (set WATCHDOG_TOKEN to enable)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[watchdog] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
