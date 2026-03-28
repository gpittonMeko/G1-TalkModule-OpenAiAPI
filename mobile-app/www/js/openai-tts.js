"use strict";

/**
 * Direct OpenAI TTS synthesis from the phone (standalone mode).
 * Calls https://api.openai.com/v1/audio/speech and returns base64 WAV/MP3.
 */
const OpenAiTts = (() => {
  const API_URL = "https://api.openai.com/v1/audio/speech";

  /**
   * Synthesize text → audio via OpenAI.
   * @param {string} text
   * @param {object} [opts] - voice, model, format overrides
   * @returns {Promise<{base64: string, format: string}>}
   */
  async function synthesize(text, opts = {}) {
    const settings = Settings.get();
    const apiKey = settings.apiKey;
    if (!apiKey) throw new Error("API Key OpenAI non configurata. Vai in Impostazioni.");

    const voice = opts.voice || settings.ttsVoice || "nova";
    const model = opts.model || settings.ttsModel || "gpt-4o-mini-tts";
    const format = opts.format || "mp3";

    const body = { model, input: text, voice, response_format: format };
    if (model === "gpt-4o-mini-tts") {
      body.instructions = "Parla in italiano con tono naturale e chiaro.";
    }

    const resp = await fetch(API_URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const err = await resp.text().catch(() => "");
      throw new Error(`OpenAI TTS errore ${resp.status}: ${err}`);
    }

    const blob = await resp.blob();
    const base64 = await _blobToBase64(blob);
    return { base64, format };
  }

  function _blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onloadend = () => {
        const dataUrl = reader.result;
        const b64 = dataUrl.split(",")[1];
        resolve(b64);
      };
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  /**
   * Check whether we have a valid-looking API key configured.
   */
  function hasKey() {
    const k = Settings.get().apiKey;
    return !!(k && k.startsWith("sk-") && k.length > 20);
  }

  return { synthesize, hasKey };
})();
