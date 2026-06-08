"use strict";

/**
 * Dashboard / Service-management tab logic.
 * Polls health, drives restart/stop/start, shows logs.
 */
const Services = (() => {
  let _pollTimer = null;
  let _connected = false;
  let _wdConnected = false;

  function isConnected() { return _connected; }

  // ── Polling ───────────────────────────────

  function startPolling() {
    stopPolling();
    _poll();
    _pollTimer = setInterval(_poll, 5000);
  }

  function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  async function _poll() {
    // Talk service health
    try {
      const h = await Api.health();
      _connected = true;
      _setStatus("svcStatus", "Attivo", "ok");
      _setConn(true);
      if (h.host) _el("svcHost").textContent = h.host;
    } catch {
      _connected = false;
      _setStatus("svcStatus", "Non raggiungibile", "err");
      _setConn(false);
      _el("svcHost").textContent = "--";
    }

    // Version (only when connected)
    if (_connected) {
      try {
        const v = await Api.version();
        _el("svcVersion").textContent = v.version || v.deploy || "--";
      } catch { _el("svcVersion").textContent = "--"; }
    } else {
      _el("svcVersion").textContent = "--";
    }

    // Watchdog (solo HTTP; su dashboard HTTPS non fare richieste mixed-content)
    if (!Settings.get().https) {
      try {
        await Api.wdHealth();
        _wdConnected = true;
        _setStatus("wdStatus", "Attivo", "ok");
      } catch {
        _wdConnected = false;
        _setStatus("wdStatus", "Non raggiungibile", "err");
      }
    } else {
      _wdConnected = false;
      _setStatus("wdStatus", "Solo HTTP", "warn");
    }

    _updateButtons();
    _updateAppMode();
  }

  // ── Actions ───────────────────────────────

  async function start() {
    if (!_wdConnected) return App.toast("Watchdog non raggiungibile");
    _disableServiceBtns(true);
    App.toast("Avvio servizio...");
    try {
      const r = await Api.wdTalkStart();
      App.toast(r.message || "Avviato");
    } catch (e) {
      App.toast("Errore avvio: " + e.message);
    }
    _disableServiceBtns(false);
    setTimeout(_poll, 2000);
  }

  async function restart() {
    if (!_wdConnected) return App.toast("Watchdog non raggiungibile");
    _disableServiceBtns(true);
    App.toast("Riavvio servizio...");
    try {
      const r = await Api.wdTalkRestart();
      App.toast(r.message || "Riavviato");
    } catch (e) {
      App.toast("Errore riavvio: " + e.message);
    }
    _disableServiceBtns(false);
    setTimeout(_poll, 3000);
  }

  async function stop() {
    if (!_wdConnected) return App.toast("Watchdog non raggiungibile");
    _disableServiceBtns(true);
    App.toast("Arresto servizio...");
    try {
      const r = await Api.wdTalkStop();
      App.toast(r.message || "Arrestato");
    } catch (e) {
      App.toast("Errore arresto: " + e.message);
    }
    _disableServiceBtns(false);
    setTimeout(_poll, 2000);
  }

  async function refreshLog() {
    const box = _el("logBox");
    if (!_wdConnected) {
      box.textContent = "Watchdog non raggiungibile.";
      return;
    }
    try {
      box.textContent = "Caricamento...";
      const r = await Api.wdTalkLog();
      box.textContent = (r.lines || []).join("\n") || "(vuoto)";
      box.scrollTop = box.scrollHeight;
    } catch (e) {
      box.textContent = "Errore: " + e.message;
    }
  }

  // ── Helpers ───────────────────────────────

  function _el(id) { return document.getElementById(id); }

  function _setStatus(id, text, cls) {
    const el = _el(id);
    el.textContent = text;
    el.className = "status-value " + (cls || "");
  }

  function _setConn(ok) {
    const dot = _el("connDot");
    const lbl = _el("connLabel");
    dot.className = "conn-dot " + (ok ? "ok" : "");
    lbl.textContent = ok ? "Connesso" : "Disconnesso";
  }

  function _updateButtons() {
    const dis = !_wdConnected;
    _el("btnStart").disabled = dis;
    _el("btnRestart").disabled = dis;
    _el("btnStop").disabled = dis;
  }

  function _updateAppMode() {
    const el = _el("appMode");
    if (_connected) {
      el.textContent = "Connesso";
      el.className = "status-value ok";
    } else {
      el.textContent = "Standalone";
      el.className = "status-value warn";
    }
  }

  function _disableServiceBtns(v) {
    _el("btnStart").disabled = v;
    _el("btnRestart").disabled = v;
    _el("btnStop").disabled = v;
  }

  return { startPolling, stopPolling, isConnected, start, restart, stop, refreshLog };
})();
