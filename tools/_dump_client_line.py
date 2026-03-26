import re
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "talk_module" / "web_app.py"
text = p.read_text(encoding="utf-8")
m = re.search(r'CLIENT_TEMPLATE = """', text)
if not m:
    raise SystemExit("CLIENT_TEMPLATE not found")
start = m.end()
rest = text[start:]
end = rest.find('"""\n\n# Local page - push-to-talk')
if end < 0:
    end = rest.find('"""\n\n# Local page')
if end < 0:
    end = rest.rfind('"""')
client = rest[:end]
lines = client.splitlines()
print("Total lines:", len(lines))
import sys
lo, hi = 620, 635
if len(sys.argv) >= 3:
    lo, hi = int(sys.argv[1]), int(sys.argv[2])
for n in range(lo, hi + 1):
    if 0 <= n - 1 < len(lines):
        print(f"{n}: {lines[n - 1]!r}")
