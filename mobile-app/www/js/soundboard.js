"use strict";

/**
 * Soundboard tab: 20-slot grid with FULL standalone support.
 *
 * Standalone workflow:
 *   1. Apri uno slot vuoto → scrivi testo + icona
 *   2. "Genera TTS" crea audio via OpenAI direttamente dal telefono
 *   3. "Salva" memorizza audio in IndexedDB (permanente, sopravvive a riavvii)
 *   4. Tap sullo slot → riproduce dalla cache locale via cassa BT
 *   5. "Genera tutti TTS" pre-genera audio per TUTTI gli slot con testo senza audio
 *
 * Non serve MAI la Jetson per riprodurre audio già memorizzati.
 */
const Soundboard = (() => {
  const SLOT_COUNT = 20;
  let _slots = [];
  let _editIdx = -1;
  let _playing = -1;
  let _audioEl = null;
  let _outputDeviceId = "";

  // ── Init ──────────────────────────────────

  async function init() {
    _slots = new Array(SLOT_COUNT).fill(null).map(() => _emptySlot());
    await _loadFromCache();
    render();
    refreshOutputs();
  }

  function _emptySlot() {
    return {
      icon: "", text: "",
      audio_base64: "", format: "mp3",
      audio_base64_clean: "", format_clean: "",
      has_robot: false, has_clean: false,
      robot_arm: "", robot_loco: "",
    };
  }

  // ── Render Grid ───────────────────────────

  function render() {
    const grid = document.getElementById("sbGrid");
    grid.innerHTML = "";
    for (let i = 0; i < SLOT_COUNT; i++) {
      const s = _slots[i] || _emptySlot();
      const hasAudio = !!(s.audio_base64 || s.audio_base64_clean);
      const hasText = !!s.text;
      const empty = !hasText && !hasAudio;

      const div = document.createElement("div");
      div.className = "sb-slot"
        + (empty ? " empty" : "")
        + (_playing === i ? " playing" : "")
        + (!hasAudio && hasText ? " no-audio" : "");
      div.dataset.idx = i;
      div.onclick = (e) => {
        if (e.target.closest(".sb-edit-badge")) return;
        _playSlot(i);
      };

      let html = "";
      if (s.robot_arm || s.robot_loco) {
        html += '<span class="sb-robot-badge">\u{1F916}</span>';
      }
      html += `<span class="sb-edit-badge" onclick="Soundboard.openModal(${i})">\u270F\uFE0F</span>`;
      html += `<span class="sb-icon">${s.icon || (empty ? "\u2795" : "\u{1F50A}")}</span>`;
      html += `<span class="sb-label">${_escHtml(s.text) || (empty ? "Vuoto" : "...")}</span>`;

      // Audio status badge
      if (hasText && hasAudio) {
        html += '<span class="sb-cached-badge">\u2705</span>';
      } else if (hasText && !hasAudio) {
        html += '<span class="sb-cached-badge" style="opacity:.7">\u26A0\uFE0F</span>';
      }

      div.innerHTML = html;
      grid.appendChild(div);
    }
  }

  // ── Playback ──────────────────────────────

  async function _playSlot(idx) {
    const s = _slots[idx];
    if (!s) return;

    const hasAudio = s.audio_base64 || s.audio_base64_clean;

    // Empty slot → open editor
    if (!hasAudio && !s.text) {
      openModal(idx);
      return;
    }

    // Has text but no cached audio → generate and cache it
    if (!hasAudio && s.text) {
      App.toast("Genero e memorizzo audio...");
      try {
        const result = await _generateTTS(s.text);
        s.audio_base64 = result.base64;
        s.format = result.format;
        await Storage.putSlot(idx, s);
        render();
        App.toast("Audio memorizzato!");
      } catch (e) {
        App.toast("Errore TTS: " + e.message);
        return;
      }
    }

    const dest = document.getElementById("sbPlayDest").value;

    if (Services.isConnected()) {
      _fireRobotActions(s);
    }

    if (dest === "server" && Services.isConnected()) {
      _playOnServer(idx, s);
    } else {
      _playLocal(idx, s);
    }
  }

  function _playLocal(idx, slot) {
    _stopCurrent();
    const b64 = slot.audio_base64_clean || slot.audio_base64;
    const fmt = slot.audio_base64_clean ? (slot.format_clean || "mp3") : (slot.format || "mp3");
    if (!b64) { App.toast("Nessun audio"); return; }

    const mime = fmt === "wav" ? "audio/wav" : "audio/mpeg";
    _audioEl = new Audio(`data:${mime};base64,${b64}`);

    if (_outputDeviceId && _audioEl.setSinkId) {
      _audioEl.setSinkId(_outputDeviceId).catch(() => {});
    }

    _playing = idx;
    render();

    _audioEl.onended = () => { _playing = -1; render(); };
    _audioEl.onerror = () => { _playing = -1; render(); App.toast("Errore riproduzione"); };
    _audioEl.play().catch((e) => { _playing = -1; render(); App.toast("Play: " + e.message); });
  }

  async function _playOnServer(idx, slot) {
    _playing = idx;
    render();
    try {
      await Api.soundboardPlayLocal(slot);
    } catch (e) {
      App.toast("Errore server: " + e.message);
    }
    setTimeout(() => { _playing = -1; render(); }, 3000);
  }

  function _stopCurrent() {
    if (_audioEl) {
      _audioEl.pause();
      _audioEl.src = "";
      _audioEl = null;
    }
    _playing = -1;
  }

  async function _fireRobotActions(slot) {
    const arm = slot.robot_arm || "face_wave";
    try { await Api.robotAction(arm); } catch {}
    if (slot.robot_loco) {
      try { await Api.robotLoco(slot.robot_loco); } catch {}
    }
  }

  // ── TTS Generation ────────────────────────

  async function _generateTTS(text) {
    // Prefer Jetson if connected (no API key needed), fallback to direct OpenAI
    if (Services.isConnected()) {
      try {
        const r = await Api.soundboardSynth(text);
        if (r.audio_base64) return { base64: r.audio_base64, format: r.format || "wav" };
      } catch {}
    }
    return OpenAiTts.synthesize(text);
  }

  /**
   * Bulk-generate TTS for ALL slots that have text but no cached audio.
   * Audio is saved permanently in IndexedDB.
   */
  async function generateAllTTS() {
    const toGen = [];
    for (let i = 0; i < SLOT_COUNT; i++) {
      const s = _slots[i];
      if (s && s.text && !s.audio_base64 && !s.audio_base64_clean) {
        toGen.push(i);
      }
    }

    if (toGen.length === 0) {
      App.toast("Tutti gli slot con testo hanno gia audio memorizzato");
      return;
    }

    if (!Services.isConnected() && !OpenAiTts.hasKey()) {
      App.toast("Serve API Key OpenAI nelle Impostazioni per generare offline");
      return;
    }

    const spinner = document.getElementById("sbGenAllSpinner");
    const progress = document.getElementById("sbGenProgress");
    spinner.style.display = "inline-block";
    progress.style.display = "block";
    document.getElementById("btnSbGenAll").disabled = true;

    let done = 0;
    let errors = 0;
    for (const idx of toGen) {
      const s = _slots[idx];
      progress.textContent = `Generazione ${done + 1}/${toGen.length}: "${s.text.slice(0, 30)}..."`;

      try {
        const result = await _generateTTS(s.text);
        s.audio_base64 = result.base64;
        s.format = result.format;
        await Storage.putSlot(idx, s);
        done++;
        render();
      } catch (e) {
        errors++;
        console.warn(`TTS slot ${idx} failed:`, e);
      }
    }

    spinner.style.display = "none";
    document.getElementById("btnSbGenAll").disabled = false;
    progress.textContent = `Completato: ${done} generati, ${errors} errori`;
    setTimeout(() => { progress.style.display = "none"; }, 5000);

    Settings.updateCacheStats();
    App.toast(`${done} audio memorizzati nel telefono`);
  }

  // ── Sync from Jetson ──────────────────────

  async function syncFromJetson() {
    if (!Services.isConnected()) {
      App.toast("Jetson non raggiungibile");
      return;
    }

    const spinner = document.getElementById("sbSyncSpinner");
    const banner = document.getElementById("sbSyncBanner");
    spinner.style.display = "inline-block";
    banner.style.display = "none";

    try {
      const lite = await Api.soundboardLite();
      const liteSlots = lite.slots || [];

      for (let i = 0; i < Math.min(liteSlots.length, SLOT_COUNT); i++) {
        const meta = liteSlots[i];
        _slots[i] = {
          ..._emptySlot(),
          icon: meta.icon || "",
          text: meta.text || "",
          format: meta.format || "mp3",
          format_clean: meta.format_clean || "",
          has_robot: !!meta.has_robot,
          has_clean: !!meta.has_clean,
          robot_arm: meta.robot_arm || "",
          robot_loco: meta.robot_loco || "",
        };
      }
      render();

      let fetched = 0;
      for (let i = 0; i < SLOT_COUNT; i++) {
        if (!_slots[i].text && !_slots[i].icon) continue;
        try {
          const full = await Api.soundboardSlot(i);
          if (full) {
            if (full.audio_base64) _slots[i].audio_base64 = full.audio_base64;
            if (full.audio_base64_clean) _slots[i].audio_base64_clean = full.audio_base64_clean;
            if (full.format) _slots[i].format = full.format;
            if (full.format_clean) _slots[i].format_clean = full.format_clean;
          }
          await Storage.putSlot(i, _slots[i]);
          fetched++;
        } catch {}
      }

      banner.className = "sync-banner";
      banner.textContent = `Sincronizzati e memorizzati ${fetched} slot (audio incluso)`;
      banner.style.display = "flex";
      App.toast("Audio scaricati e memorizzati nel telefono");
      render();
      Settings.updateCacheStats();

    } catch (e) {
      banner.className = "sync-banner err";
      banner.textContent = "Errore sync: " + e.message;
      banner.style.display = "flex";
    }

    spinner.style.display = "none";
  }

  // ── Cache Load ────────────────────────────

  async function _loadFromCache() {
    try {
      const cached = await Storage.getAllSlots();
      for (const c of cached) {
        if (c.idx >= 0 && c.idx < SLOT_COUNT) {
          _slots[c.idx] = { ..._emptySlot(), ...c };
        }
      }
    } catch {}
  }

  // ── Audio Output ──────────────────────────

  async function refreshOutputs() {
    const sel = document.getElementById("sbAudioOut");
    sel.innerHTML = '<option value="">Default</option>';
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      for (const d of devices) {
        if (d.kind !== "audiooutput") continue;
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        opt.textContent = d.label || `Dispositivo ${d.deviceId.slice(0, 8)}`;
        sel.appendChild(opt);
      }
    } catch {}
    sel.value = _outputDeviceId;
    sel.onchange = () => { _outputDeviceId = sel.value; };
  }

  // ── Modal (Edit Slot) ─────────────────────

  function openModal(idx) {
    _editIdx = idx;
    const s = _slots[idx] || _emptySlot();
    document.getElementById("sbModalIdx").textContent = `#${idx + 1}`;
    document.getElementById("sbmIcon").value = s.icon || "";
    document.getElementById("sbmText").value = s.text || "";
    document.getElementById("sbmRobotArm").value = s.robot_arm || "";
    document.getElementById("sbmRobotLoco").value = s.robot_loco || "";
    _updateModalAudioInfo(s);
    document.getElementById("sbModal").classList.add("open");
  }

  function closeModal() {
    document.getElementById("sbModal").classList.remove("open");
    _editIdx = -1;
  }

  function _updateModalAudioInfo(s) {
    const el = document.getElementById("sbmAudioInfo");
    if (s.audio_base64 || s.audio_base64_clean) {
      const b64 = s.audio_base64 || s.audio_base64_clean;
      const kb = Math.round((b64.length * 3) / 4 / 1024);
      el.innerHTML = `<span style="color:var(--green)">\u2705 Audio memorizzato</span> (${s.format || "?"}, ~${kb} KB)`;
    } else if (s.text) {
      el.innerHTML = `<span style="color:var(--yellow)">\u26A0\uFE0F Solo testo, audio non generato</span> &mdash; premi "Genera TTS" per memorizzare`;
    } else {
      el.textContent = "Nessun contenuto";
    }
  }

  async function modalGenerateTTS() {
    const text = document.getElementById("sbmText").value.trim();
    if (!text) { App.toast("Inserisci un testo"); return; }

    const spinner = document.getElementById("sbmTtsSpinner");
    spinner.style.display = "inline-block";
    try {
      const result = await _generateTTS(text);
      if (_editIdx >= 0) {
        _slots[_editIdx].audio_base64 = result.base64;
        _slots[_editIdx].format = result.format;
        _updateModalAudioInfo(_slots[_editIdx]);
      }
      App.toast("Audio generato e pronto per il salvataggio");
    } catch (e) {
      App.toast("Errore: " + e.message);
    }
    spinner.style.display = "none";
  }

  function modalPlayPreview() {
    if (_editIdx < 0) return;
    const s = _slots[_editIdx];
    if (!s || !s.audio_base64) { App.toast("Nessun audio — genera prima il TTS"); return; }
    _stopCurrent();
    const mime = (s.format === "wav") ? "audio/wav" : "audio/mpeg";
    const audio = new Audio(`data:${mime};base64,${s.audio_base64}`);
    if (_outputDeviceId && audio.setSinkId) {
      audio.setSinkId(_outputDeviceId).catch(() => {});
    }
    audio.play().catch((e) => App.toast("Play: " + e.message));
  }

  /**
   * Save slot: if text present but no audio, auto-generate TTS before saving.
   */
  async function modalSave() {
    if (_editIdx < 0) return;
    const s = _slots[_editIdx];
    s.icon = document.getElementById("sbmIcon").value.trim();
    s.text = document.getElementById("sbmText").value.trim();
    s.robot_arm = document.getElementById("sbmRobotArm").value.trim();
    s.robot_loco = document.getElementById("sbmRobotLoco").value.trim();

    // Auto-generate TTS if text changed and no audio yet
    if (s.text && !s.audio_base64 && !s.audio_base64_clean) {
      App.toast("Genero audio automaticamente...");
      try {
        const result = await _generateTTS(s.text);
        s.audio_base64 = result.base64;
        s.format = result.format;
      } catch (e) {
        App.toast("TTS fallito: " + e.message + " — slot salvato senza audio");
      }
    }

    await Storage.putSlot(_editIdx, s);

    if (Services.isConnected()) {
      try { await Api.soundboardSave(_slots); } catch {}
    }

    render();
    closeModal();
    Settings.updateCacheStats();
    App.toast(s.audio_base64 ? "Slot salvato con audio memorizzato" : "Slot salvato (solo testo)");
  }

  async function modalClear() {
    if (_editIdx < 0) return;
    _slots[_editIdx] = _emptySlot();
    await Storage.deleteSlot(_editIdx);
    render();
    closeModal();
    Settings.updateCacheStats();
    App.toast("Slot svuotato");
  }

  // ── Helpers ───────────────────────────────

  function _escHtml(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  return {
    init, render, syncFromJetson, generateAllTTS, refreshOutputs,
    openModal, closeModal, modalGenerateTTS, modalPlayPreview, modalSave, modalClear,
  };
})();
