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
  function soundboardPlayLocal(data) { return _post(`${_cfg().talk}/api/soundboard-play-local`, data); }
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

  function wdHealth()     { return _json(`${_cfg().wd}/health`, { headers: _wdHeaders() }); }
  function wdTalkStatus() { return _json(`${_cfg().wd}/talk-status`, { headers: _wdHeaders() }); }
  function wdTalkLog()    { return _json(`${_cfg().wd}/talk-log`, { headers: _wdHeaders() }); }

  function wdTalkRestart() {
    return _json(`${_cfg().wd}/talk-restart`, { method: "POST", headers: _wdHeaders(), timeout: 60000 });
  }
  function wdTalkStop() {
    return _json(`${_cfg().wd}/talk-stop`, { method: "POST", headers: _wdHeaders() });
  }
  function wdTalkStart() {
    return _json(`${_cfg().wd}/talk-start`, { method: "POST", headers: _wdHeaders(), timeout: 60000 });
  }

  // ── Reachability ──────────────────────────

  async function isReachable() {
    try { await _fetch(`${_cfg().talk}/api/health`, { timeout: 3000 }); return true; }
    catch { return false; }
  }
  async function isWatchdogReachable() {
    try { await _fetch(`${_cfg().wd}/health`, { timeout: 3000, headers: _wdHeaders() }); return true; }
    catch { return false; }
  }

  return {
    health, version, sttInfo,
    soundboardLite, soundboardFull, soundboardSlot, soundboardPlayLocal,
    soundboardSaveSlot, soundboardSynth,
    robotActions, robotAction, robotMove, robotLoco,
    ledEffect, ledState, ledColor, ledAnimation,
    textChat,
    wdHealth, wdTalkStatus, wdTalkRestart, wdTalkStop, wdTalkStart, wdTalkLog,
    isReachable, isWatchdogReachable,
  };
})();
