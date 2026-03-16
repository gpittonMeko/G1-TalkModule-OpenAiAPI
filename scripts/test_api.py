#!/usr/bin/env python3
"""Test API text-chat."""
import requests

url = "http://127.0.0.1:8081/api/text-chat"
r = requests.post(url, json={"text": "Come ti chiami?"}, timeout=30)
print("Status:", r.status_code)
d = r.json()
print("Response:", (d.get("response") or "")[:80])
print("Duration:", d.get("duration_ms"), "ms")
print("OK" if d.get("response") else "FAIL")
