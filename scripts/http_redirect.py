#!/usr/bin/env python3
"""HTTP su TALK_HTTP_REDIRECT_PORT (default 8080) → redirect a HTTPS su TALK_PUBLIC_HOST:TALK_HTTPS_PORT."""
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

# Default storico: AI Accelerator. Su Jetson imposta TALK_PUBLIC_HOST in .env (es. 192.168.123.164).
_HOST = os.environ.get("TALK_PUBLIC_HOST", "192.168.10.191").strip() or "192.168.10.191"
_HTTPS_PORT = os.environ.get("TALK_HTTPS_PORT", "8081").strip() or "8081"
_HTTP_PORT = int(os.environ.get("TALK_HTTP_REDIRECT_PORT", "8080") or "8080")


class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", f"https://{_HOST}:{_HTTPS_PORT}{self.path}")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", _HTTP_PORT), Redirect).serve_forever()
