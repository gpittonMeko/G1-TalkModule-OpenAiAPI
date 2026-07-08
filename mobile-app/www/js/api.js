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
    // Watchdog Jetson è solo HTTP (:8082): su dashboard HTTPS evita mixed-content.
    const wd = s.https ? null : `http://${s.ip}:${s.wdPort}`;
    return {
      talk: `${proto}://${s.ip}:${s.port}`,
      wd,
      token: s.wdToken || "",
    };
  }

  function _base() { return _cfg().talk; }

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

  function _post(url, body, opts) {
    return _json(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      ...opts,
    });
  }

  // ── Talk Service (:8081) ──────────────────

  function health()  { return _json(`${_cfg().talk}/api/health`); }
  function version() { return _json(`${_cfg().talk}/api/version`); }
  function sttInfo() { return _json(`${_cfg().talk}/api/stt-info`); }

  // Soundboard
  function soundboardLite()       { return _json(`${_cfg().talk}/api/soundboard?lite=1`); }
  function soundboardFull()       { return _json(`${_cfg().talk}/api/soundboard`); }
  function soundboardSlot(idx)    { return _json(`${_cfg().talk}/api/soundboard-slot/${idx}`); }
  function soundboardPlayLocal(data) {
    return _post(`${_cfg().talk}/api/soundboard-play-local`, data, { timeout: 180000 });
  }
  function soundboardSynth(text)  { return _post(`${_cfg().talk}/api/soundboard-synth`, { text }); }

  /**
   * Save a single soundboard slot. Server tiene solo la traccia clean.
   */
  function soundboardSaveSlot(slotIdx, slotData) {
    const clean = slotData.audio_base64_clean || slotData.audio_base64 || "";
    const fc = slotData.format_clean || slotData.format || "mp3";
    return _post(`${_cfg().talk}/api/soundboard`, {
      slot: slotIdx,
      audio_base64: "",
      format: fc,
      audio_base64_clean: clean,
      format_clean: fc,
      text: slotData.text || "",
      icon: slotData.icon || "",
      robot_arm: slotData.robot_arm || "",
      robot_loco: slotData.robot_loco || "",
      led_effect: slotData.led_effect || "",
      teaching_slot: slotData.teaching_slot || "",
    });
  }

  // Robot
  function robotActions() { return _json(`${_cfg().talk}/api/robot-actions`); }

  function robotAction(actionId, robotIp) {
    const body = { action_id: actionId };
    if (robotIp) body.robot_ip = robotIp;
    return _post(`${_cfg().talk}/api/robot-action`, body);
  }

  function robotMove(vx, vy, vyaw, robotIp) {
    const body = { vx, vy, vyaw };
    if (robotIp) body.robot_ip = robotIp;
    return _post(`${_cfg().talk}/api/robot-move`, body);
  }

  function robotLoco(command, robotIp) {
    const body = { command };
    if (robotIp) body.robot_ip = robotIp;
    return _post(`${_cfg().talk}/api/robot-loco`, body);
  }

  // LED
  function ledEffect(effect)   { return _post(`${_cfg().talk}/api/led`, { effect }); }
  function ledState(state)     { return _post(`${_cfg().talk}/api/led`, { state }); }
  function ledColor(r, g, b)   { return _post(`${_cfg().talk}/api/led`, { r, g, b }); }
  function ledAnimation(anim, color, speed) {
    return _post(`${_cfg().talk}/api/led`, { animation: anim, color, speed });
  }

  // Camera + YOLO
  function cameraStatus() { return _json(`${_cfg().talk}/api/camera/status`); }
  function cameraStart()   { return _post(`${_cfg().talk}/api/camera/start`, {}); }
  function cameraStop()    { return _post(`${_cfg().talk}/api/camera/stop`, {}); }
  function cameraStreamUrl() { return `${_cfg().talk}/api/camera/stream`; }
  function serverLog(lines = 120) {
    return _json(`${_cfg().talk}/api/server-log?lines=${lines}`);
  }

  // Text chat
  function textChat(text) {
    return _post(`${_cfg().talk}/api/text-chat`, { text }, { timeout: 30000 });
  }

  // ── Watchdog (:8082) ──────────────────────

  function _wdHeaders() {
    const h = {};
    const t = _cfg().token;
    if (t) h["Authorization"] = `Bearer ${t}`;
    return h;
  }

  function _wdUrl(path) {
    const wd = _cfg().wd;
    if (!wd) return null;
    return `${wd}${path}`;
  }

  function wdHealth() {
    const u = _wdUrl("/health");
    if (!u) return Promise.reject(new Error("Watchdog non disponibile su HTTPS"));
    return _json(u, { headers: _wdHeaders() });
  }
  function wdTalkStatus() {
    const u = _wdUrl("/talk-status");
    if (!u) return Promise.reject(new Error("Watchdog non disponibile su HTTPS"));
    return _json(u, { headers: _wdHeaders() });
  }
  function wdTalkLog() {
    const u = _wdUrl("/talk-log");
    if (!u) return Promise.reject(new Error("Watchdog non disponibile su HTTPS"));
    return _json(u, { headers: _wdHeaders() });
  }

  function wdTalkRestart() {
    const u = _wdUrl("/talk-restart");
    if (!u) return Promise.reject(new Error("Watchdog non disponibile su HTTPS"));
    return _json(u, { method: "POST", headers: _wdHeaders(), timeout: 60000 });
  }
  function wdTalkStop() {
    const u = _wdUrl("/talk-stop");
    if (!u) return Promise.reject(new Error("Watchdog non disponibile su HTTPS"));
    return _json(u, { method: "POST", headers: _wdHeaders() });
  }
  function wdTalkStart() {
    const u = _wdUrl("/talk-start");
    if (!u) return Promise.reject(new Error("Watchdog non disponibile su HTTPS"));
    return _json(u, { method: "POST", headers: _wdHeaders(), timeout: 60000 });
  }

  // ── Reachability ──────────────────────────

  async function isReachable() {
    try { await _fetch(`${_cfg().talk}/api/health`, { timeout: 3000 }); return true; }
    catch { return false; }
  }
  async function isWatchdogReachable() {
    if (!_cfg().wd) return false;
    try { await _fetch(`${_cfg().wd}/health`, { timeout: 3000, headers: _wdHeaders() }); return true; }
    catch { return false; }
  }

  return {
    _base,
    health, version, sttInfo,
    soundboardLite, soundboardFull, soundboardSlot, soundboardPlayLocal,
    soundboardSaveSlot, soundboardSynth,
    robotActions, robotAction, robotMove, robotLoco,
    ledEffect, ledState, ledColor, ledAnimation,
    cameraStatus, cameraStart, cameraStop, cameraStreamUrl, serverLog,
    textChat,
    wdHealth, wdTalkStatus, wdTalkRestart, wdTalkStop, wdTalkStart, wdTalkLog,
    isReachable, isWatchdogReachable,
  };
})();
