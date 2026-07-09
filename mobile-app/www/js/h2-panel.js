"use strict";

/** H2 arm/hand controls + diagnostic log panels on Dashboard tab. */
const H2Panel = (() => {
  let _armPoll = null;

  function syncArmLabels() {
    const r = document.getElementById("armRise");
    const h = document.getElementById("armHold");
    const l = document.getElementById("armLower");
    if (r) document.getElementById("armRiseVal").textContent = r.value;
    if (h) document.getElementById("armHoldVal").textContent = h.value;
    if (l) document.getElementById("armLowerVal").textContent = l.value;
  }

  function syncHandLabel() {
    const g = document.getElementById("handGrip");
    if (g) document.getElementById("handGripVal").textContent = g.value;
  }

  function _appendLog(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.scrollTop = el.scrollHeight;
  }

  async function refreshChannel(channel, boxId) {
    try {
      const r = await Api.h2Logs(channel, 40);
      _appendLog(boxId, (r.lines || []).join("\n") || "(vuoto)");
    } catch (e) {
      _appendLog(boxId, "Errore log: " + e.message);
    }
  }

  async function refreshAllDiagLogs() {
    await Promise.all([
      refreshChannel("opencv", "logOpenCV"),
      refreshChannel("movement", "logMovement"),
      refreshChannel("hand", "logHand"),
      refreshChannel("tts", "logTTS"),
    ]);
  }

  async function refreshH2Status() {
    try {
      const s = await Api.h2Status();
      const handEl = document.getElementById("handStatus");
      if (handEl) {
        if (s.h2_lab_available) {
          handEl.textContent = "Lab H2 OK";
          handEl.className = "status-value ok";
        } else {
          handEl.textContent = s.platform === "windows" ? "Solo UI (Windows)" : "Non disponibile";
          handEl.className = "status-value warn";
        }
      }
      return s;
    } catch (e) {
      const handEl = document.getElementById("handStatus");
      if (handEl) {
        handEl.textContent = "Offline";
        handEl.className = "status-value err";
      }
      return null;
    }
  }

  async function refreshTtsEnvStatus() {
    try {
      const h = await Api.health();
      const el = document.getElementById("ttsEnvStatus");
      const setEl = document.getElementById("setEnvKeyStatus");
      const ok = !!h.openai_configured;
      const txt = ok ? "Configurata" : "Mancante in .env";
      const cls = ok ? "status-value ok" : "status-value err";
      if (el) { el.textContent = txt; el.className = cls; }
      if (setEl) { setEl.textContent = txt; setEl.className = cls; }
    } catch {
      const el = document.getElementById("ttsEnvStatus");
      if (el) { el.textContent = "Server offline"; el.className = "status-value err"; }
    }
  }

  function _pollArmStatus() {
    if (_armPoll) clearInterval(_armPoll);
    _armPoll = setInterval(async () => {
      try {
        const s = await Api.h2ArmStatus();
        const el = document.getElementById("armRunStatus");
        if (!el) return;
        if (s.running) {
          el.textContent = "In esecuzione...";
          el.className = "status-value warn";
        } else if (s.exit_code === 0) {
          el.textContent = "Completato";
          el.className = "status-value ok";
          clearInterval(_armPoll);
          _armPoll = null;
        } else if (s.exit_code != null) {
          el.textContent = "Errore exit " + s.exit_code;
          el.className = "status-value err";
          clearInterval(_armPoll);
          _armPoll = null;
        }
        if (s.log && s.log.length) {
          _appendLog("logMovement", s.log.join("\n"));
        }
      } catch {}
    }, 2000);
  }

  async function armMove() {
    const s = await refreshH2Status();
    if (s && !s.h2_lab_available) {
      return App.toast(s.detail || "Movimento H2 non disponibile su questo server");
    }
    syncArmLabels();
    try {
      await Api.h2ArmMove({
        rise: parseFloat(document.getElementById("armRise").value),
        hold: parseFloat(document.getElementById("armHold").value),
        lower: parseFloat(document.getElementById("armLower").value),
      });
      App.toast("Movimento braccio avviato");
      _pollArmStatus();
      setTimeout(() => refreshChannel("movement", "logMovement"), 1500);
    } catch (e) {
      App.toast("Errore: " + e.message);
      refreshChannel("movement", "logMovement");
    }
  }

  async function armStop() {
    try {
      await Api.h2ArmStop();
      App.toast("STOP inviato");
      refreshChannel("movement", "logMovement");
    } catch (e) {
      App.toast("Errore STOP: " + e.message);
    }
  }

  async function wakePc2() {
    App.toast("Wake servizio mani PC2...");
    try {
      const r = await Api.h2WakePc2();
      App.toast(r.ok ? "Servizio mani avviato" : "Wake fallito — vedi log");
      refreshChannel("hand", "logHand");
      refreshH2Status();
    } catch (e) {
      App.toast("Errore wake: " + e.message);
      refreshChannel("hand", "logHand");
    }
  }

  async function handProbe() {
    try {
      const r = await Api.h2HandProbe();
      App.toast(r.ok ? "hand-dds OK" : "hand-dds FAIL");
      refreshChannel("hand", "logHand");
    } catch (e) {
      App.toast("Probe errore: " + e.message);
      refreshChannel("hand", "logHand");
    }
  }

  async function handGrip(openHand) {
    syncHandLabel();
    const frac = parseInt(document.getElementById("handGrip").value, 10) / 100;
    try {
      const r = await Api.h2HandGrip({ side: "left", close_fraction: frac, open_hand: openHand });
      App.toast(r.ok ? (openHand ? "Mano aperta" : "Grip eseguito") : "Grip fallito");
      refreshChannel("hand", "logHand");
    } catch (e) {
      App.toast("Errore grip: " + e.message);
      refreshChannel("hand", "logHand");
    }
  }

  async function testTtsServer() {
    App.toast("Test TTS server...");
    try {
      const r = await Api.ttsTest("Test voce G1 Talk da dashboard.");
      App.toast(r.ok ? `TTS OK (${r.bytes} bytes)` : "TTS fallito");
      refreshChannel("tts", "logTTS");
      refreshTtsEnvStatus();
    } catch (e) {
      App.toast("TTS errore: " + e.message);
      refreshChannel("tts", "logTTS");
    }
  }

  function onDashboardShow() {
    syncArmLabels();
    syncHandLabel();
    refreshH2Status();
    refreshTtsEnvStatus();
    refreshAllDiagLogs();
  }

  return {
    syncArmLabels,
    syncHandLabel,
    armMove,
    armStop,
    wakePc2,
    handProbe,
    handGrip,
    testTtsServer,
    refreshAllDiagLogs,
    refreshTtsEnvStatus,
    onDashboardShow,
  };
})();
