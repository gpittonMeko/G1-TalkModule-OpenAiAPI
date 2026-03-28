"use strict";

/**
 * Settings tab logic: persists config, drives UI.
 */
const Settings = (() => {
  const DEFAULTS = {
    ip: "192.168.123.164",
    port: 8081,
    wdPort: 8082,
    https: false,
    apiKey: "",
    ttsVoice: "nova",
    ttsModel: "gpt-4o-mini-tts",
    wdToken: "",
  };

  let _cfg = { ...DEFAULTS };

  function init() {
    const saved = Storage.loadSettings();
    if (saved) _cfg = { ...DEFAULTS, ...saved };
    _toUI();
    updateCacheStats();
  }

  function get() { return { ..._cfg }; }

  function save() {
    _fromUI();
    Storage.saveSettings(_cfg);
    App.toast("Impostazioni salvate");
    Services.startPolling();
  }

  function setIp(ip) {
    document.getElementById("setIp").value = ip;
    _cfg.ip = ip;
  }

  function toggleHttps() {
    const el = document.getElementById("setHttps");
    _cfg.https = !_cfg.https;
    el.classList.toggle("on", _cfg.https);
  }

  function toggleKeyVis() {
    const inp = document.getElementById("setApiKey");
    inp.type = inp.type === "password" ? "text" : "password";
  }

  async function clearCache() {
    if (!confirm("Svuotare tutta la cache della soundboard?")) return;
    await Storage.clearSlots();
    sessionStorage.removeItem("g1tr_sb_autosync_attempted");
    await Soundboard.init();
    updateCacheStats();
    App.toast("Cache svuotata");
  }

  async function updateCacheStats() {
    try {
      const stats = await Storage.cacheStats();
      document.getElementById("cacheSlots").textContent = stats.count;
      const kb = Math.round(stats.sizeBytes / 1024);
      document.getElementById("cacheSize").textContent = kb > 1024
        ? (kb / 1024).toFixed(1) + " MB"
        : kb + " KB";
    } catch {}
  }

  function _toUI() {
    document.getElementById("setIp").value = _cfg.ip;
    document.getElementById("setPort").value = _cfg.port;
    document.getElementById("setWdPort").value = _cfg.wdPort;
    document.getElementById("setWdToken").value = _cfg.wdToken;
    document.getElementById("setHttps").classList.toggle("on", _cfg.https);
    document.getElementById("setApiKey").value = _cfg.apiKey;
    document.getElementById("setTtsVoice").value = _cfg.ttsVoice;
    document.getElementById("setTtsModel").value = _cfg.ttsModel;
  }

  function _fromUI() {
    _cfg.ip = document.getElementById("setIp").value.trim() || DEFAULTS.ip;
    _cfg.port = parseInt(document.getElementById("setPort").value) || DEFAULTS.port;
    _cfg.wdPort = parseInt(document.getElementById("setWdPort").value) || DEFAULTS.wdPort;
    _cfg.wdToken = document.getElementById("setWdToken").value.trim();
    _cfg.apiKey = document.getElementById("setApiKey").value.trim();
    _cfg.ttsVoice = document.getElementById("setTtsVoice").value;
    _cfg.ttsModel = document.getElementById("setTtsModel").value;
  }

  return { init, get, save, setIp, toggleHttps, toggleKeyVis, clearCache, updateCacheStats };
})();
