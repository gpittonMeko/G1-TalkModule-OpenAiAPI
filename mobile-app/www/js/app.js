"use strict";

/**
 * Main app controller: tab routing, init, global helpers.
 */
const App = (() => {
  let _toastTimer = null;

  async function init() {
    Settings.init();
    await Soundboard.init();
    Services.startPolling();
    if (typeof CameraPanel !== "undefined") CameraPanel.onDashboardShow();
    _restoreTab();
  }

  // ── Tab Navigation ────────────────────────

  function switchTab(name) {
    document.querySelectorAll(".tab-page").forEach(p => p.classList.remove("active"));
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));

    const page = document.getElementById("tab-" + name);
    const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
    if (page) page.classList.add("active");
    if (btn) btn.classList.add("active");

    localStorage.setItem("g1tr_tab", name);

    if (name === "soundboard") {
      Soundboard.render();
      Soundboard.maybeAutoSyncFromJetson();
    }
    if (name === "dashboard" && typeof CameraPanel !== "undefined") {
      CameraPanel.onDashboardShow();
    }
    if (name === "settings") Settings.updateCacheStats();
    if (name === "robot") RobotPanel.loadActions();
  }

  function _restoreTab() {
    const saved = localStorage.getItem("g1tr_tab");
    if (saved) switchTab(saved);
  }

  // ── Toast ─────────────────────────────────

  function toast(msg, duration) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove("show"), duration || 2500);
  }

  function _baseUrl() {
    const s = Settings.get();
    const proto = s.https ? "https" : "http";
    return `${proto}://${s.ip}:${s.port}`;
  }

  /** Apre una pagina sul server Jetson (path con / iniziale). */
  function openPath(path) {
    window.open(_baseUrl() + path, "_blank");
  }

  /** Web UI principale /client; sezione opzionale (#parla, #soundboard, …). */
  function openWebUI(hash) {
    const h = hash && String(hash).replace(/^#/, "") ? "#" + String(hash).replace(/^#/, "") : "";
    window.open(_baseUrl() + "/client" + h, "_blank");
  }

  /** Joystick + gesti + Teaching (braccia) sulla stessa pagina. */
  function openRobotControl() {
    openPath("/robot-control");
  }

  // ── Boot ──────────────────────────────────

  document.addEventListener("DOMContentLoaded", init);

  return { switchTab, toast, openWebUI, openRobotControl, openPath };
})();
