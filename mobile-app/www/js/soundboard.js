"use strict";

/**
 * Soundboard tab: 20-slot grid with FULL standalone support.
 *
 * Standalone workflow:
 *   1. Apri uno slot vuoto -> scrivi testo + icona
 *   2. "Genera TTS" crea audio via OpenAI direttamente dal telefono
 *   3. "Salva" memorizza audio in IndexedDB (permanente)
 *   4. Tap sullo slot -> riproduce dalla cache locale via cassa BT
 *   5. "Genera tutti TTS" pre-genera audio per TUTTI gli slot con testo
 *
 * Non serve MAI la Jetson per riprodurre audio gia memorizzati.
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
    updateSyncHint();
  }

  function _emptySlot() {
    return {
      icon: "", text: "",
      audio_base64: "", format: "mp3",
      audio_base64_clean: "", format_clean: "",
      has_robot: false, has_clean: false,
      robot_arm: "", robot_loco: "", led_effect: "", teaching_slot: "",
    };
  }

  /** Solo traccia clean: migra vecchia cache e azzera audio_base64. */
  function _normalizeSlotAudio(s) {
    if (!s) return s;
    if (!s.audio_base64_clean && s.audio_base64) {
      s.audio_base64_clean = s.audio_base64;
      s.format_clean = s.format_clean || s.format || "mp3";
    }
    s.audio_base64 = "";
    s.has_robot = false;
    s.has_clean = !!(s.audio_base64_clean && String(s.audio_base64_clean).length > 50);
    return s;
  }

  // ── Render Grid ───────────────────────────

  function render() {
    const grid = document.getElementById("sbGrid");
    grid.innerHTML = "";
    for (let i = 0; i < SLOT_COUNT; i++) {
      _slots[i] = _normalizeSlotAudio(_slots[i] || _emptySlot());
      const s = _slots[i];
      const hasAudio = !!(s.audio_base64_clean && String(s.audio_base64_clean).length > 50);
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
      if (s.robot_arm || s.robot_loco) html += '<span class="sb-robot-badge">\u{1F916}</span>';
      html += `<span class="sb-edit-badge" onclick="Soundboard.openModal(${i})">\u270F\uFE0F</span>`;
      html += `<span class="sb-icon">${s.icon || (empty ? "\u2795" : "\u{1F50A}")}</span>`;
      html += `<span class="sb-label">${_esc(s.text) || (empty ? "Vuoto" : "...")}</span>`;
      if (hasText && hasAudio)  html += '<span class="sb-cached-badge">\u2705</span>';
      else if (hasText)         html += '<span class="sb-cached-badge" style="opacity:.7">\u26A0\uFE0F</span>';
      div.innerHTML = html;
      grid.appendChild(div);
    }
  }

  // ── Playback ──────────────────────────────

  async function _playSlot(idx) {
    const s = _slots[idx];
    if (!s) return;
    const hasAudio = s.audio_base64_clean && String(s.audio_base64_clean).length > 50;

    if (!hasAudio && !s.text) { openModal(idx); return; }

    if (!hasAudio && s.text) {
      App.toast("Genero e memorizzo audio...");
      try {
        const result = await _generateTTS(s.text);
        s.audio_base64 = "";
        s.audio_base64_clean = result.base64;
        s.format_clean = result.format;
        s.format = result.format;
        _normalizeSlotAudio(s);
        await Storage.putSlot(idx, s);
        render();
        App.toast("Audio memorizzato!");
      } catch (e) { App.toast("Errore TTS: " + e.message); return; }
    }

    const dest = document.getElementById("sbPlayDest").value;

    // Robot actions + LED when connected
    if (Services.isConnected()) {
      _fireRobotActions(s);
      if (s.led_effect) {
        try { await Api.ledEffect(s.led_effect); } catch {}
      }
    }

    if (dest === "server" && Services.isConnected()) {
      _playOnServer(idx, s);
    } else {
      _playLocal(idx, s);
    }
  }

  function _playLocal(idx, slot) {
    _stopCurrent();
    const b64 = slot.audio_base64_clean;
    const fmt = slot.format_clean || "mp3";
    if (!b64) { App.toast("Nessun audio"); return; }

    const mime = fmt === "wav" ? "audio/wav" : "audio/mpeg";
    _audioEl = new Audio(`data:${mime};base64,${b64}`);
    if (_outputDeviceId && _audioEl.setSinkId) {
      _audioEl.setSinkId(_outputDeviceId).catch(() => {});
    }
    _playing = idx; render();
    _audioEl.onended = () => { _playing = -1; render(); };
    _audioEl.onerror = () => {
      _playing = -1; render();
      _fallbackRobotSpeaker(idx, slot, "Errore riproduzione telefono");
    };
    _audioEl.play().catch((e) => {
      _playing = -1; render();
      _fallbackRobotSpeaker(idx, slot, "Play telefono: " + e.message);
    });
  }

  async function _fallbackRobotSpeaker(idx, slot, reason) {
    if (!Services.isConnected()) {
      App.toast(reason);
      return;
    }
    App.toast(reason + " → cassa robot...");
    await _playOnServer(idx, slot);
  }

  async function _playOnServer(idx, slot) {
    _playing = idx; render();
    try {
      const r = await Api.soundboardPlayLocal({ slot: idx });
      if (r && r.backend === "g1_internal") {
        App.toast("Riproduzione cassa interna G1");
      }
    } catch (e) { App.toast("Errore cassa robot: " + e.message); }
    finally { _playing = -1; render(); }
  }

  function _stopCurrent() {
    if (_audioEl) { _audioEl.pause(); _audioEl.src = ""; _audioEl = null; }
    _playing = -1;
  }

  async function _fireRobotActions(slot) {
    if (slot.teaching_slot != null && slot.teaching_slot !== "") {
      try {
        await fetch(Api._base() + "/api/teaching/replay_slot/" + slot.teaching_slot, { method: "POST" });
      } catch {}
    }
    const arm = slot.robot_arm || "face_wave";
    try { await Api.robotAction(arm); } catch {}
    if (slot.robot_loco) {
      try { await Api.robotLoco(slot.robot_loco); } catch {}
    }
  }

  // ── TTS Generation ────────────────────────

  async function _generateTTS(text) {
    if (Services.isConnected()) {
      try {
        const r = await Api.soundboardSynth(text);
        if (r && r.ok !== false) {
          const b = r.audio_base64_clean || r.audio_base64;
          if (b) return { base64: b, format: r.format_clean || r.format || "wav" };
        }
      } catch {}
    }
    if (!OpenAiTts.hasKey()) {
      throw new Error(
        "Robot senza internet: sincronizza slot con audio oppure metti API Key OpenAI in Impostazioni"
      );
    }
    return OpenAiTts.synthesize(text);
  }

  async function generateAllTTS() {
    const toGen = [];
    for (let i = 0; i < SLOT_COUNT; i++) {
      const s = _slots[i];
      if (s && s.text && !(s.audio_base64_clean && String(s.audio_base64_clean).length > 50)) toGen.push(i);
    }
    if (!toGen.length) { App.toast("Tutti gli slot con testo hanno gia audio memorizzato"); return; }
    if (!Services.isConnected() && !OpenAiTts.hasKey()) {
      App.toast("Serve API Key OpenAI nelle Impostazioni per generare offline");
      return;
    }

    const spinner = document.getElementById("sbGenAllSpinner");
    const progress = document.getElementById("sbGenProgress");
    spinner.style.display = "inline-block";
    progress.style.display = "block";
    document.getElementById("btnSbGenAll").disabled = true;

    let done = 0, errors = 0;
    for (const idx of toGen) {
      const s = _slots[idx];
      progress.textContent = `Generazione ${done + 1}/${toGen.length}: "${s.text.slice(0, 30)}..."`;
      try {
        const result = await _generateTTS(s.text);
        s.audio_base64 = "";
        s.audio_base64_clean = result.base64;
        s.format_clean = result.format;
        s.format = s.format_clean;
        _normalizeSlotAudio(s);
        await Storage.putSlot(idx, s);
        done++;
        render();
      } catch { errors++; }
    }

    spinner.style.display = "none";
    document.getElementById("btnSbGenAll").disabled = false;
    progress.textContent = `Completato: ${done} generati` + (errors ? `, ${errors} errori` : "");
    setTimeout(() => { progress.style.display = "none"; }, 5000);
    Settings.updateCacheStats();
    App.toast(`${done} audio memorizzati nel telefono`);
  }

  // ── Sync from Jetson ──────────────────────

  async function syncFromJetson() {
    if (!Services.isConnected()) { App.toast("Jetson non raggiungibile"); return; }

    const spinner = document.getElementById("sbSyncSpinner");
    const banner = document.getElementById("sbSyncBanner");
    spinner.style.display = "inline-block";
    banner.style.display = "none";

    try {
      const lite = await Api.soundboardLite();
      const liteSlots = lite.slots || [];

      for (let i = 0; i < Math.min(liteSlots.length, SLOT_COUNT); i++) {
        const m = liteSlots[i];
        _slots[i] = {
          ..._emptySlot(),
          icon: m.icon || "", text: m.text || "",
          format: m.format || "mp3", format_clean: m.format_clean || "",
          has_robot: !!m.has_robot, has_clean: !!m.has_clean,
          robot_arm: m.robot_arm || "", robot_loco: m.robot_loco || "",
          led_effect: m.led_effect || "", teaching_slot: m.teaching_slot || "",
        };
      }
      render();

      let fetched = 0;
      for (let i = 0; i < SLOT_COUNT; i++) {
        const si = _slots[i];
        const hasMeta = si.text || si.icon || si.has_robot || si.has_clean;
        if (!hasMeta) continue;
        try {
          const full = await Api.soundboardSlot(i);
          if (full) {
            _slots[i].audio_base64 = "";
            _slots[i].audio_base64_clean = full.audio_base64_clean || full.audio_base64 || "";
            _slots[i].format_clean = full.format_clean || full.format || "mp3";
            _slots[i].format = _slots[i].format_clean;
            if (full.led_effect !== undefined) _slots[i].led_effect = full.led_effect || "";
            _normalizeSlotAudio(_slots[i]);
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
    updateSyncHint();
  }

  /**
   * Prima volta con cache vuota e Jetson raggiungibile: scarica soundboard automaticamente.
   */
  async function maybeAutoSyncFromJetson() {
    updateSyncHint();
    if (!Services.isConnected()) return;
    const has = await Storage.hasAnySlotContent();
    if (has) return;
    // Una sola prova automatica per sessione browser (evita loop se la sync fallisce)
    if (sessionStorage.getItem("g1tr_sb_autosync_attempted") === "1") return;
    sessionStorage.setItem("g1tr_sb_autosync_attempted", "1");
    App.toast("Prima sync dalla Jetson in corso...");
    await syncFromJetson();
  }

  function updateSyncHint() {
    const el = document.getElementById("sbSyncHint");
    if (!el) return;
    Storage.hasAnySlotContent().then((has) => {
      const ok = Services.isConnected();
      if (has) {
        el.style.display = "none";
        return;
      }
      el.style.display = "block";
      if (ok) {
        el.innerHTML = "Cache <strong>vuota</strong>: tocca <strong>Sincronizza</strong> se la sync automatica non è partita, oppure attendi il completamento.";
      } else {
        el.innerHTML = "Cache <strong>vuota</strong>: connettiti alla stessa rete della Jetson (IP in Impostazioni, stato <strong>Connesso</strong>), poi apri di nuovo questa tab o premi <strong>Sincronizza</strong>.";
      }
    }).catch(() => {});
  }

  // ── Cache ─────────────────────────────────

  async function _loadFromCache() {
    try {
      const cached = await Storage.getAllSlots();
      for (const c of cached) {
        if (c.idx >= 0 && c.idx < SLOT_COUNT) _slots[c.idx] = _normalizeSlotAudio({ ..._emptySlot(), ...c });
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
    document.getElementById("sbmLedEffect").value = s.led_effect || "";
    _updateModalAudioInfo(s);
    document.getElementById("sbModal").classList.add("open");
  }

  function closeModal() {
    document.getElementById("sbModal").classList.remove("open");
    _editIdx = -1;
  }

  function _updateModalAudioInfo(s) {
    const el = document.getElementById("sbmAudioInfo");
    if (s.audio_base64_clean && String(s.audio_base64_clean).length > 50) {
      const kb = Math.round((s.audio_base64_clean.length * 3) / 4 / 1024);
      el.innerHTML = `<span style="color:var(--green)">\u2705 Audio memorizzato</span> (${s.format_clean || "?"}, ~${kb} KB)`;
    } else if (s.text) {
      el.innerHTML = `<span style="color:var(--yellow)">\u26A0\uFE0F Solo testo</span> &mdash; premi "Genera TTS"`;
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
        _slots[_editIdx].audio_base64 = "";
        _slots[_editIdx].audio_base64_clean = result.base64;
        _slots[_editIdx].format_clean = result.format;
        _slots[_editIdx].format = result.format;
        _normalizeSlotAudio(_slots[_editIdx]);
        _updateModalAudioInfo(_slots[_editIdx]);
      }
      App.toast("Audio generato");
    } catch (e) { App.toast("Errore: " + e.message); }
    spinner.style.display = "none";
  }

  function modalPlayPreview() {
    if (_editIdx < 0) return;
    const s = _slots[_editIdx];
    if (!s || !s.audio_base64_clean) { App.toast("Nessun audio — genera prima il TTS"); return; }
    _stopCurrent();
    const mime = (s.format_clean === "wav") ? "audio/wav" : "audio/mpeg";
    const audio = new Audio(`data:${mime};base64,${s.audio_base64_clean}`);
    if (_outputDeviceId && audio.setSinkId) audio.setSinkId(_outputDeviceId).catch(() => {});
    audio.play().catch((e) => App.toast("Play: " + e.message));
  }

  async function modalSave() {
    if (_editIdx < 0) return;
    const s = _slots[_editIdx];
    s.icon = document.getElementById("sbmIcon").value.trim();
    s.text = document.getElementById("sbmText").value.trim();
    s.robot_arm = document.getElementById("sbmRobotArm").value.trim();
    s.robot_loco = document.getElementById("sbmRobotLoco").value.trim();
    s.led_effect = document.getElementById("sbmLedEffect").value.trim();

    // Auto-generate TTS if text present but no audio
    if (s.text && !(s.audio_base64_clean && String(s.audio_base64_clean).length > 50)) {
      App.toast("Genero audio automaticamente...");
      try {
        const result = await _generateTTS(s.text);
        s.audio_base64 = "";
        s.audio_base64_clean = result.base64;
        s.format_clean = result.format;
        s.format = result.format;
        _normalizeSlotAudio(s);
      } catch (e) {
        App.toast("TTS fallito: " + e.message + " — salvato senza audio");
      }
    }

    await Storage.putSlot(_editIdx, s);

    // Push to Jetson if connected (single-slot save)
    if (Services.isConnected()) {
      try { await Api.soundboardSaveSlot(_editIdx, s); } catch {}
    }

    render(); closeModal();
    Settings.updateCacheStats();
    App.toast(s.audio_base64_clean && String(s.audio_base64_clean).length > 50 ? "Slot salvato con audio" : "Slot salvato (solo testo)");
  }

  async function modalClear() {
    if (_editIdx < 0) return;
    _slots[_editIdx] = _emptySlot();
    await Storage.deleteSlot(_editIdx);
    render(); closeModal();
    Settings.updateCacheStats();
    App.toast("Slot svuotato");
  }

  function _esc(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  return {
    init, render, syncFromJetson, generateAllTTS, refreshOutputs,
    maybeAutoSyncFromJetson, updateSyncHint,
    openModal, closeModal, modalGenerateTTS, modalPlayPreview, modalSave, modalClear,
  };
})();
