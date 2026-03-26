import re
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "talk_module" / "web_app.py"
text = p.read_text(encoding="utf-8")
m = re.search(r'CLIENT_TEMPLATE = """(.*?)"""', text, re.S)
if not m:
    raise SystemExit("no CLIENT_TEMPLATE")
body = m.group(1)
lines = body.splitlines()
print("total lines", len(lines))
for idx in range(623, 636):  # 0-based: line 624 = file line 625
    if idx < len(lines):
        L = lines[idx]
        print(f"{idx + 1}: col35={repr(L[34:42]) if len(L) > 34 else 'SHORT'} | {L[:100]}")
