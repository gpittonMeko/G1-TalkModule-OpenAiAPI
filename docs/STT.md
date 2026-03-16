# STT (Speech-to-Text) – Provider e troubleshooting

## Provider disponibili

| Provider | Vantaggi | Setup |
|----------|----------|-------|
| **whisper** | Già configurato con OpenAI | `OPENAI_API_KEY` |
| **groq** | Veloce, gratuito, stesso modello Whisper | `GROQ_API_KEY` |
| **deepgram** | Meno allucinazioni, WER migliore | `DEEPGRAM_API_KEY` |

## Configurazione

In `.env`:

```env
# Groq (consigliato, veloce)
STT_PROVIDER=groq
GROQ_API_KEY=la_tua_chiave

# Deepgram
STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=la_tua_chiave
```

Installazione pacchetti:
```bash
pip install groq        # per Groq
pip install deepgram-sdk  # per Deepgram
```

## Correzioni fuzzy

Lo STT può trascrivere male (es. "prava" invece di "prova"). Il modulo fuzzy corregge automaticamente usando:

- `config/knowledge.json` – pattern della knowledge
- `config/italian_vocabulary.txt` – parole italiane comuni
- `config/stt_config.json` – extra_phrases, threshold

## Troubleshooting

### "Nessun testo riconosciuto"
1. Parla 1–2 secondi, vicino al microfono
2. Controlla la barra livello (deve muoversi)
3. Prova `STT_PROVIDER=groq` o `deepgram`

### "Audio non chiaro" / allucinazioni Whisper
- Whisper con audio poco chiaro può restituire "Sottotitoli Amara.org" (filtrato)
- Usa Groq o Deepgram per risultati migliori

### Debug
- Audio non trascritti: `temp/audio/debug_*.webm`
- Endpoint: `GET /api/debug-audio`
- Provider attivo: `GET /api/stt-info`
