from pathlib import Path
p = Path(__file__).resolve().parent.parent / "talk_module" / "web_app.py"
for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
    if "nessun dato" in line and "textContent" in line:
        print("line", i, repr(line))
