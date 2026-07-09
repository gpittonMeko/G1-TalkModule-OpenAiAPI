"use strict";

/**
 * Dashboard / Service-management tab logic.
 * Polls health, drives restart/stop/start, shows logs.
 */
const Services = (() => {
  let _pollTimer = null;
  let _logTimer = null;
  let _connected = false;
  let _wdConnected = false;
  let _openaiOk = false;
  let _logFilter = "all";

  function isConnected() { return _connected; }

  function startPolling() {
    stopPolling();
    _poll();
    _pollTimer = setInterval(_poll, 5000);
    startLogAutoRefresh();
  }

  function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    stopLogAutoRefresh();
  }

  function startLogAutoRefresh() {
    stopLogAutoRefresh();
    refreshLog();
    _logTimer = setInterval(refreshLog, 5000);
    if (typeof H2Panel !== "undefined") {
      setInterval(() => {
        if (document.getElementById("tab-dashboard")?.classList.contains("active")) {
          H2Panel.refreshAllDiagLogs();
        }
      }, 8000);
    }
  }

  function stopLogAutoRefresh() {
    if (_logTimer) { clearInterval(_logTimer); _logTimer = null; }
  }

  async function _poll() {
    try {
      const h = await Api.health();
      _connected = true;
      _openaiOk = !!h.openai_configured;
      _setStatus("svcStatus", "Attivo", "ok");
      _setConn(true);
      if (h.host) _el("svcHost").textContent = h.host;
    } catch {
      _connected = false;
      _openaiOk = false;
      _setStatus("svcStatus", "Non raggiungibile", "err");
      _setConn(false);
      _el("svcHost").textContent = "--";
    }

    if (_connected) {
      try {
        const v = await Api.version();
        _el("svcVersion").textContent = v.version || v.deploy || "--";
      } catch { _el("svcVersion").textContent = "--"; }
    } else {
      _el("svcVersion").textContent = "--";
    }

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

    _updateDiagBanner();
    _updateButtons();
    _updateAppMode();
    if (typeof H2Panel !== "undefined") H2Panel.refreshTtsEnvStatus();
  }

  function _updateDiagBanner() {
    const b = _el("diagBanner");
    if (!b) return;
    if (!_connected) {
      b.className = "diag-banner err";
      b.textContent = "DISCONNESSO — controlla IP/porta in Impostazioni (Thor H2: 192.168.123.163, locale: 127.0.0.1)";
      return;
    }
    if (!_openaiOk) {
      b.className = "diag-banner warn";
      b.textContent = "SERVER OK ma OPENAI MANCANTE — imposta OPENAI_API_KEY nel .env e riavvia";
      return;
    }
    if (Settings.get().https && !_wdConnected) {
      b.className = "diag-banner warn";
      b.textContent = "HTTPS: watchdog disabilitato. Usa log API / Avvia da SSH se serve.";
      return;
    }
    b.className = "diag-banner ok";
    b.textContent = "Talk online — OpenAI configurata";
  }

  function setLogFilter(filter, btn) {
    _logFilter = filter;
    document.querySelectorAll(".filter-chip").forEach(c => c.classList.remove("active"));
    if (btn) btn.classList.add("active");
    refreshLog();
  }

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
    if (!box) return;
    const ch = _logFilter === "all" ? "" : _logFilter;
    try {
      const r = await Api.serverLog(150, ch);
      box.textContent = (r.lines || []).join("\n") || "(vuoto)";
      box.scrollTop = box.scrollHeight;
      return;
    } catch (_) {}
    if (!_wdConnected) {
      if (_connected) {
        box.textContent = "(usa filtri OpenCV/Movimento/TTS sopra se log generale vuoto)";
      } else {
        box.textContent = "Log non disponibile — server offline.";
      }
      return;
    }
    try {
      const r = await Api.wdTalkLog();
      box.textContent = (r.lines || []).join("\n") || "(vuoto)";
      box.scrollTop = box.scrollHeight;
    } catch (e) {
      box.textContent = "Errore: " + e.message;
    }
  }

  async function copyLog() {
    const box = _el("logBox");
    if (!box) return;
    try {
      await navigator.clipboard.writeText(box.textContent || "");
      App.toast("Log copiato");
    } catch {
      App.toast("Copia non riuscita");
    }
  }

  function _el(id) { return document.getElementById(id); }

  function _setStatus(id, text, cls) {
    const el = _el(id);
    if (!el) return;
    el.textContent = text;
    el.className = "status-value " + (cls || "");
  }

  function _setConn(ok) {
    const dot = _el("connDot");
    const lbl = _el("connLabel");
    if (dot) dot.className = "conn-dot " + (ok ? "ok" : "");
    if (lbl) lbl.textContent = ok ? "Connesso" : "Disconnesso";
  }

  function _updateButtons() {
    const dis = !_wdConnected;
    ["btnStart", "btnRestart", "btnStop"].forEach(id => {
      const el = _el(id);
      if (el) el.disabled = dis;
    });
  }

  function _updateAppMode() {
    const el = _el("appMode");
    if (!el) return;
    if (_connected) {
      el.textContent = "Connesso";
      el.className = "status-value ok";
    } else {
      el.textContent = "Standalone";
      el.className = "status-value warn";
    }
  }

  function _disableServiceBtns(v) {
    ["btnStart", "btnRestart", "btnStop"].forEach(id => {
      const el = _el(id);
      if (el) el.disabled = v;
    });
  }

  return {
    startPolling, stopPolling, isConnected,
    start, restart, stop, refreshLog, copyLog, setLogFilter,
  };
})();
