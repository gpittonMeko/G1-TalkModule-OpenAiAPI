"""
Installer grafico G1 Talk Module.
Wizard che chiede solo la chiave OpenAI e completa l'installazione.
"""

import os
import sys
import webbrowser
import threading
from pathlib import Path

# Root del progetto (parent di installer/)
ROOT = Path(__file__).resolve().parent.parent


def _load_env_example() -> str:
    p = ROOT / ".env.example"
    return p.read_text(encoding="utf-8") if p.exists() else "OPENAI_API_KEY="


def _save_env(api_key: str, language: str = "it") -> bool:
    """Salva .env con la chiave e opzioni."""
    env_path = ROOT / ".env"
    template = _load_env_example()
    lines = []
    key_set = False
    for line in template.splitlines():
        if line.strip().startswith("OPENAI_API_KEY="):
            lines.append(f"OPENAI_API_KEY={api_key.strip()}")
            key_set = True
        elif line.strip().startswith("TTS_LANGUAGE="):
            lines.append(f"TTS_LANGUAGE={language}")
        else:
            lines.append(line)
    if not key_set:
        lines.insert(0, f"OPENAI_API_KEY={api_key.strip()}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def run_installer(port: int = 9999):
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn

    app = FastAPI(title="G1 Talk Installer")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return WIZARD_HTML

    @app.post("/api/save")
    def save(api_key: str = Form(...), language: str = Form("it")):
        key = (api_key or "").strip()
        if not key or len(key) < 20:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Inserisci una chiave API valida (inizia con sk-)"},
            )
        try:
            _save_env(key, language)
            return {"ok": True}
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    @app.get("/api/check")
    def check():
        env = ROOT / ".env"
        has_key = False
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OPENAI_API_KEY=") and "sk-" in line:
                    has_key = True
                    break
        return {"configured": has_key}

    @app.post("/api/done")
    def done():
        """Chiude l'installer dopo configurazione."""
        import os
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return {"ok": True}

    def open_browser():
        import time
        time.sleep(1.2)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass

    if os.environ.get("DISPLAY") or sys.platform != "linux":
        threading.Thread(target=open_browser, daemon=True).start()
    print(f"\n  Installer: http://127.0.0.1:{port}/")
    print("  Apri nel browser se non si apre automaticamente.\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


WIZARD_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G1 Talk - Installazione</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'DM Sans', system-ui, sans-serif;
      background: linear-gradient(135deg, #0c0c0f 0%, #1a1a24 50%, #0f0f18 100%);
      min-height: 100vh;
      color: #e4e4e7;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      background: rgba(24, 24, 27, 0.95);
      border: 1px solid rgba(63, 63, 70, 0.6);
      border-radius: 24px;
      padding: 48px 40px;
      max-width: 440px;
      width: 100%;
      box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
    }
    .logo {
      font-size: 28px;
      font-weight: 700;
      background: linear-gradient(135deg, #22c55e, #3b82f6);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      margin-bottom: 8px;
    }
    .sub {
      color: #71717a;
      font-size: 14px;
      margin-bottom: 32px;
    }
    label {
      display: block;
      font-size: 13px;
      font-weight: 500;
      color: #a1a1aa;
      margin-bottom: 8px;
    }
    input[type="text"], input[type="password"] {
      width: 100%;
      padding: 14px 18px;
      border-radius: 12px;
      border: 2px solid #3f3f46;
      background: #18181b;
      color: #fff;
      font-size: 15px;
      font-family: inherit;
      transition: border-color 0.2s;
    }
    input:focus {
      outline: none;
      border-color: #22c55e;
    }
    input::placeholder { color: #52525b; }
    .field { margin-bottom: 20px; }
    .toggle-wrap {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
    }
    .toggle-wrap input { width: auto; }
    .btn {
      width: 100%;
      padding: 16px 24px;
      border-radius: 12px;
      border: none;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
      transition: transform 0.15s, box-shadow 0.15s;
    }
    .btn-primary {
      background: linear-gradient(135deg, #22c55e, #16a34a);
      color: white;
      margin-top: 8px;
    }
    .btn-primary:hover {
      transform: translateY(-1px);
      box-shadow: 0 8px 24px rgba(34, 197, 94, 0.35);
    }
    .btn-primary:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }
    .msg {
      padding: 12px 16px;
      border-radius: 10px;
      font-size: 14px;
      margin-top: 16px;
      display: none;
    }
    .msg.error { background: rgba(220, 38, 38, 0.2); color: #fca5a5; }
    .msg.success { background: rgba(34, 197, 94, 0.2); color: #86efac; }
    .done {
      text-align: center;
      padding: 24px 0;
    }
    .done .icon { font-size: 48px; margin-bottom: 16px; }
    .done h2 { font-size: 20px; margin-bottom: 8px; }
    .done p { color: #71717a; font-size: 14px; line-height: 1.6; }
    .done .btn { margin-top: 24px; max-width: 200px; margin-left: auto; margin-right: auto; display: block; }
    .step { display: none; }
    .step.active { display: block; }
    .hint { font-size: 12px; color: #52525b; margin-top: 6px; }
    a { color: #22c55e; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">G1 Talk Module</div>
    <div class="sub">Assistente vocale per Unitree G1</div>

    <div id="step1" class="step active">
      <p style="margin-bottom:20px;color:#a1a1aa;line-height:1.6;">Per completare l'installazione serve solo la tua chiave API OpenAI. La trovi su <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>.</p>
      <form id="form">
        <div class="field">
          <label for="key">Chiave API OpenAI</label>
          <input type="password" id="key" name="api_key" placeholder="sk-..." autocomplete="off" required>
          <p class="hint">Inizia con sk- e resta privata (solo su questa macchina)</p>
        </div>
        <div class="field">
          <label>Lingua risposte</label>
          <select id="lang" name="language" style="width:100%;padding:14px 18px;border-radius:12px;border:2px solid #3f3f46;background:#18181b;color:#fff;font-size:15px;">
            <option value="it">Italiano</option>
            <option value="en">English</option>
            <option value="es">Español</option>
            <option value="de">Deutsch</option>
            <option value="fr">Français</option>
          </select>
        </div>
        <button type="submit" class="btn btn-primary" id="btnSave">Completa installazione</button>
      </form>
      <div id="msg" class="msg"></div>
    </div>

    <div id="step2" class="step">
      <div class="done">
        <div class="icon">✓</div>
        <h2>Installazione completata</h2>
        <p>Puoi avviare G1 Talk con:<br><code style="background:#27272a;padding:4px 8px;border-radius:6px;font-size:13px;">bash scripts/restart_server.sh</code></p>
        <p style="margin-top:12px;">Oppure: <code style="background:#27272a;padding:4px 8px;border-radius:6px;">python3 -m talk_module.web_app --host 0.0.0.0 --port 8081</code></p>
        <p style="margin-top:16px;">Poi apri <a href="http://localhost:8081/client" target="_blank">http://localhost:8081/client</a></p>
        <button type="button" class="btn btn-primary" onclick="closeInstaller()">Chiudi installer</button>
      </div>
    </div>
  </div>

  <script>
    const form = document.getElementById('form');
    const msg = document.getElementById('msg');
    const btn = document.getElementById('btnSave');
    const step1 = document.getElementById('step1');
    const step2 = document.getElementById('step2');

    form.onsubmit = async (e) => {
      e.preventDefault();
      const key = document.getElementById('key').value.trim();
      const lang = document.getElementById('lang').value;
      if (!key || key.length < 20) {
        msg.className = 'msg error';
        msg.textContent = 'Inserisci una chiave valida (inizia con sk-)';
        msg.style.display = 'block';
        return;
      }
      btn.disabled = true;
      msg.style.display = 'none';
      try {
        const fd = new FormData();
        fd.append('api_key', key);
        fd.append('language', lang);
        const r = await fetch('/api/save', { method: 'POST', body: fd });
        const d = await r.json();
        if (d.ok) {
          step1.classList.remove('active');
          step2.classList.add('active');
        } else {
          msg.className = 'msg error';
          msg.textContent = d.error || 'Errore salvataggio';
          msg.style.display = 'block';
          btn.disabled = false;
        }
      } catch (err) {
        msg.className = 'msg error';
        msg.textContent = 'Errore di connessione: ' + err.message;
        msg.style.display = 'block';
        btn.disabled = false;
      }
    };

    async function closeInstaller() {
      await fetch('/api/done', { method: 'POST' });
    }
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    run_installer(port)
