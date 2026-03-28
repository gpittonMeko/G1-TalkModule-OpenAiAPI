"use strict";

/**
 * Robot & Chat tab: text chat with LLM, arm actions catalog,
 * locomotion commands, LED control.
 */
const RobotPanel = (() => {
  let _chatAudioB64 = "";
  let _chatAudioFmt = "mp3";
  let _actionsLoaded = false;

  // ── Text Chat ─────────────────────────────

  async function sendChat() {
    const input = document.getElementById("chatInput");
    const text = input.value.trim();
    if (!text) { App.toast("Scrivi qualcosa"); return; }

    if (!Services.isConnected()) {
      App.toast("Jetson non raggiungibile per la chat");
      return;
    }

    const spinner = document.getElementById("chatSpinner");
    const respDiv = document.getElementById("chatResponse");
    const respText = document.getElementById("chatRespText");
    spinner.style.display = "inline-block";
    document.getElementById("btnChatSend").disabled = true;

    try {
      const r = await Api.textChat(text);
      respText.textContent = r.response || r.message || "(vuoto)";
      _chatAudioB64 = r.audio_base64 || "";
      _chatAudioFmt = "mp3";
      respDiv.style.display = "block";
      document.getElementById("btnChatPlay").style.display = _chatAudioB64 ? "inline-flex" : "none";

      if (_chatAudioB64) {
        playChatAudio();
      }
    } catch (e) {
      respText.textContent = "Errore: " + e.message;
      respDiv.style.display = "block";
      _chatAudioB64 = "";
    }

    spinner.style.display = "none";
    document.getElementById("btnChatSend").disabled = false;
  }

  function playChatAudio() {
    if (!_chatAudioB64) { App.toast("Nessun audio"); return; }
    const audio = new Audio(`data:audio/mpeg;base64,${_chatAudioB64}`);
    audio.play().catch((e) => App.toast("Play: " + e.message));
  }

  // ── Robot Actions Catalog ─────────────────

  async function loadActions() {
    if (_actionsLoaded) return;
    if (!Services.isConnected()) return;

    const grid = document.getElementById("robotActionsGrid");
    try {
      const r = await Api.robotActions();
      const arms = r.arm_actions || [];
      if (!arms.length) {
        grid.innerHTML = '<span style="font-size:12px;color:var(--text2)">Nessuna azione disponibile</span>';
        return;
      }

      grid.innerHTML = "";
      for (const a of arms) {
        const btn = document.createElement("button");
        btn.className = "btn btn-ghost btn-sm";
        btn.textContent = (a.icon || "") + " " + (a.label || a.name || a.id);
        btn.onclick = () => _fireAction(a.id || a.name);
        grid.appendChild(btn);
      }
      _actionsLoaded = true;
    } catch (e) {
      grid.innerHTML = `<span style="font-size:12px;color:var(--red)">Errore: ${e.message}</span>`;
    }
  }

  async function _fireAction(actionId) {
    App.toast("Azione: " + actionId);
    try {
      const r = await Api.robotAction(actionId);
      App.toast(r.message || "OK");
    } catch (e) {
      App.toast("Errore: " + e.message);
    }
  }

  // ── Locomotion ────────────────────────────

  async function loco(command) {
    if (!Services.isConnected()) { App.toast("Jetson non raggiungibile"); return; }
    try {
      const r = await Api.robotLoco(command);
      App.toast(r.message || command);
    } catch (e) {
      App.toast("Errore: " + e.message);
    }
  }

  // ── LED ───────────────────────────────────

  async function led(effect) {
    if (!Services.isConnected()) { App.toast("Jetson non raggiungibile"); return; }
    try {
      await Api.ledEffect(effect);
      App.toast("LED: " + effect);
    } catch (e) {
      App.toast("LED errore: " + e.message);
    }
  }

  async function ledState(state) {
    if (!Services.isConnected()) { App.toast("Jetson non raggiungibile"); return; }
    try {
      await Api.ledState(state);
      App.toast("LED: " + state);
    } catch (e) {
      App.toast("LED errore: " + e.message);
    }
  }

  return { sendChat, playChatAudio, loadActions, loco, led, ledState };
})();
