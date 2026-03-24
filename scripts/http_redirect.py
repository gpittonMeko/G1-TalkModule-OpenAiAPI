#!/usr/bin/env python3
"""Server HTTP su 8080 che reindirizza a HTTPS 8081. Per chi digita http://."""
from http.server import HTTPServer, BaseHTTPRequestHandler

class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", f"https://192.168.10.191:8081{self.path}")
        self.end_headers()
    def log_message(self, *args): pass

HTTPServer(("0.0.0.0", 8080), Redirect).serve_forever()
