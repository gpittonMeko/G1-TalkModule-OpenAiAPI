"use strict";

/**
 * Dashboard: stream MJPEG camera + YOLO dal Jetson.
 */
const CameraPanel = (() => {
  let _streaming = false;
  let _statusTimer = null;

  function _el(id) {
    return document.getElementById(id);
  }

  function _streamUrl() {
    return Api._base() + "/api/camera/stream?_=" + Date.now();
  }

  function _showStream(on) {
    const img = _el("camStream");
    const ph = _el("camPlaceholder");
    if (!img) return;
    if (on) {
      img.style.display = "block";
      if (ph) ph.style.display = "none";
      img.src = _streamUrl();
      _streaming = true;
    } else {
      img.style.display = "none";
      img.removeAttribute("src");
      if (ph) ph.style.display = "flex";
      _streaming = false;
    }
  }

  async function _pollStatus() {
    try {
      const s = await Api.cameraStatus();
      const st = _el("camStatus");
      const ys = _el("camYoloStatus");
      const fps = _el("camFps");
      const det = _el("camDetections");
      if (st) {
        if (s.open_error) {
          st.textContent = "Errore: " + s.open_error;
          st.className = "status-value err";
        } else if (s.running && s.has_frame) {
          st.textContent = (s.backend || "ok") + " " + (s.resolution || "");
          st.className = "status-value ok";
        } else if (s.running) {
          st.textContent = "Avvio…";
          st.className = "status-value warn";
        } else {
          st.textContent = "Ferma";
          st.className = "status-value";
        }
      }
      if (ys) {
        if (!s.yolo_enabled) {
          ys.textContent = "Disabilitato";
        } else if (s.yolo_loaded) {
          ys.textContent = s.yolo_model || "YOLO ok";
          ys.className = "status-value ok";
        } else if (s.yolo_error) {
          ys.textContent = "YOLO: " + String(s.yolo_error).slice(0, 40);
          ys.className = "status-value err";
        } else {
          ys.textContent = "Caricamento…";
          ys.className = "status-value warn";
        }
      }
      if (fps) fps.textContent = s.fps ? String(s.fps) : "--";
      if (det) {
        const list = s.detections || [];
        det.textContent = list.length
          ? list
              .map((d) => {
                let t = d.class + " " + Math.round((d.confidence || 0) * 100) + "%";
                if (d.depth_m != null) t += " · " + d.depth_m + "m";
                return t;
              })
              .join(" · ")
          : "Nessun oggetto rilevato";
      }
    } catch (e) {
      const st = _el("camStatus");
      if (st) {
        st.textContent = "API non raggiungibile";
        st.className = "status-value err";
      }
    }
  }

  function _startStatusPoll() {
    if (_statusTimer) return;
    _pollStatus();
    _statusTimer = setInterval(_pollStatus, 2000);
  }

  function _stopStatusPoll() {
    if (_statusTimer) {
      clearInterval(_statusTimer);
      _statusTimer = null;
    }
  }

  async function start() {
    try {
      await Api.cameraStart();
      _showStream(true);
      _startStatusPoll();
      App.toast("Stream camera avviato");
    } catch (e) {
      App.toast("Camera: " + e.message);
    }
  }

  async function stop() {
    try {
      await Api.cameraStop();
    } catch (_) {}
    _showStream(false);
    _stopStatusPoll();
    await _pollStatus();
    App.toast("Stream fermato");
  }

  function refresh() {
    if (_streaming) {
      const img = _el("camStream");
      if (img) img.src = _streamUrl();
    }
    _pollStatus();
  }

  function onDashboardShow() {
    _pollStatus();
  }

  return { start, stop, refresh, onDashboardShow };
})();
