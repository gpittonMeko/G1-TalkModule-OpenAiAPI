"use strict";

/**
 * HTTP client for Jetson Talk service (:8081) and Watchdog (:8082).
 * All methods return Promises. Connection settings come from Settings.get().
 */
const Api = (() => {
  const TIMEOUT = 6000;

  function _cfg() {
    const s = Settings.get();
    const proto = s.https ? "https" : "http";
    return {
      talk: `${proto}://${s.ip}:${s.port}`,
      wd:   `http://${s.ip}:${s.wdPort}`,
      token: s.wdToken || "",
    };
  }

  async function _fetch(url, opts = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opts.timeout || TIMEOUT);
    try {
      const resp = await fetch(url, {
        ...opts,
        signal: ctrl.signal,
        mode: "cors",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return resp;
    } finally {
      clearTimeout(timer);
    }
  }

  async function _json(url, opts) {
    const r = await _fetch(url, opts);
    return r.json();
  }

  // ── Talk Service (:8081) ──────────────────

  async function health() {
    return _json(`${_cfg().talk}/api/health`);
  }

  async function version() {
    return _json(`${_cfg().talk}/api/version`);
  }

  async function soundboardLite() {
    return _json(`${_cfg().talk}/api/soundboard?lite=1`);
  }

  async function soundboardSlot(idx) {
    return _json(`${_cfg().talk}/api/soundboard-slot/${idx}`);
  }

  async function soundboardPlayLocal(slotData) {
    return _json(`${_cfg().talk}/api/soundboard-play-local`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot: slotData }),
    });
  }

  async function soundboardSave(slots) {
    return _json(`${_cfg().talk}/api/soundboard`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slots }),
    });
  }

  async function soundboardSynth(text) {
    return _json(`${_cfg().talk}/api/soundboard-synth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  }

  async function robotAction(action) {
    return _json(`${_cfg().talk}/api/robot-action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
  }

  async function robotLoco(command) {
    return _json(`${_cfg().talk}/api/robot-loco`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command }),
    });
  }

  // ── Watchdog (:8082) ──────────────────────

  function _wdHeaders() {
    const h = {};
    const t = _cfg().token;
    if (t) h["Authorization"] = `Bearer ${t}`;
    return h;
  }

  async function wdHealth() {
    return _json(`${_cfg().wd}/health`, { headers: _wdHeaders() });
  }

  async function wdTalkStatus() {
    return _json(`${_cfg().wd}/talk-status`, { headers: _wdHeaders() });
  }

  async function wdTalkRestart() {
    return _json(`${_cfg().wd}/talk-restart`, {
      method: "POST",
      headers: _wdHeaders(),
      timeout: 60000,
    });
  }

  async function wdTalkStop() {
    return _json(`${_cfg().wd}/talk-stop`, {
      method: "POST",
      headers: _wdHeaders(),
    });
  }

  async function wdTalkStart() {
    return _json(`${_cfg().wd}/talk-start`, {
      method: "POST",
      headers: _wdHeaders(),
      timeout: 60000,
    });
  }

  async function wdTalkLog() {
    return _json(`${_cfg().wd}/talk-log`, { headers: _wdHeaders() });
  }

  /**
   * Quick reachability check: resolves true/false, never throws.
   */
  async function isReachable() {
    try {
      await _fetch(`${_cfg().talk}/api/health`, { timeout: 3000 });
      return true;
    } catch { return false; }
  }

  async function isWatchdogReachable() {
    try {
      await _fetch(`${_cfg().wd}/health`, { timeout: 3000, headers: _wdHeaders() });
      return true;
    } catch { return false; }
  }

  return {
    health, version, soundboardLite, soundboardSlot,
    soundboardPlayLocal, soundboardSave, soundboardSynth,
    robotAction, robotLoco,
    wdHealth, wdTalkStatus, wdTalkRestart, wdTalkStop, wdTalkStart, wdTalkLog,
    isReachable, isWatchdogReachable,
  };
})();
