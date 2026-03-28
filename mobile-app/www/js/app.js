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

    if (name === "soundboard") Soundboard.render();
    if (name === "settings") Settings.updateCacheStats();
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

  // ── Quick Links ───────────────────────────

  function openWebUI() {
    const s = Settings.get();
    const proto = s.https ? "https" : "http";
    const url = `${proto}://${s.ip}:${s.port}/client`;
    window.open(url, "_blank");
  }

  function openRobotControl() {
    const s = Settings.get();
    const proto = s.https ? "https" : "http";
    const url = `${proto}://${s.ip}:${s.port}/robot-control`;
    window.open(url, "_blank");
  }

  // ── Boot ──────────────────────────────────

  document.addEventListener("DOMContentLoaded", init);

  return { switchTab, toast, openWebUI, openRobotControl };
})();
