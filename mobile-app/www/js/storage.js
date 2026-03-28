"use strict";

/**
 * IndexedDB wrapper for local soundboard cache and settings persistence.
 * DB: "g1talk_remote", stores: "slots", "meta"
 */
const Storage = (() => {
  const DB_NAME = "g1talk_remote";
  const DB_VERSION = 1;
  let _db = null;

  function _open() {
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains("slots")) {
          db.createObjectStore("slots", { keyPath: "idx" });
        }
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", { keyPath: "key" });
        }
      };
      req.onsuccess = () => { _db = req.result; resolve(_db); };
      req.onerror = () => reject(req.error);
    });
  }

  async function _tx(store, mode) {
    const db = await _open();
    return db.transaction(store, mode).objectStore(store);
  }

  function _req(idbReq) {
    return new Promise((resolve, reject) => {
      idbReq.onsuccess = () => resolve(idbReq.result);
      idbReq.onerror = () => reject(idbReq.error);
    });
  }

  // ── Slots ─────────────────────────────────

  async function getSlot(idx) {
    const s = await _tx("slots", "readonly");
    return _req(s.get(idx));
  }

  async function putSlot(idx, data) {
    const s = await _tx("slots", "readwrite");
    return _req(s.put({ idx, ...data, _ts: Date.now() }));
  }

  async function deleteSlot(idx) {
    const s = await _tx("slots", "readwrite");
    return _req(s.delete(idx));
  }

  async function getAllSlots() {
    const s = await _tx("slots", "readonly");
    return _req(s.getAll());
  }

  async function clearSlots() {
    const s = await _tx("slots", "readwrite");
    return _req(s.clear());
  }

  async function countSlots() {
    const s = await _tx("slots", "readonly");
    return _req(s.count());
  }

  /**
   * Returns {count, sizeBytes} for cache stats display.
   */
  async function cacheStats() {
    const all = await getAllSlots();
    let bytes = 0;
    for (const s of all) {
      const json = JSON.stringify(s);
      bytes += json.length * 2; // rough char→byte estimate
    }
    return { count: all.length, sizeBytes: bytes };
  }

  /** True se esiste almeno uno slot con contenuto utile (cache non vuota). */
  async function hasAnySlotContent() {
    const all = await getAllSlots();
    for (const r of all) {
      if (r.text || r.audio_base64 || r.audio_base64_clean || (r.icon && String(r.icon).trim())) {
        return true;
      }
    }
    return false;
  }

  // ── Meta (key-value) ──────────────────────

  async function getMeta(key) {
    const s = await _tx("meta", "readonly");
    const r = await _req(s.get(key));
    return r ? r.value : null;
  }

  async function putMeta(key, value) {
    const s = await _tx("meta", "readwrite");
    return _req(s.put({ key, value }));
  }

  // ── Settings (localStorage convenience) ───

  const SETTINGS_KEY = "g1tr_settings";

  function loadSettings() {
    try {
      const raw = localStorage.getItem(SETTINGS_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  }

  function saveSettings(obj) {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(obj));
  }

  return {
    getSlot, putSlot, deleteSlot, getAllSlots, clearSlots, countSlots, cacheStats, hasAnySlotContent,
    getMeta, putMeta,
    loadSettings, saveSettings,
  };
})();
