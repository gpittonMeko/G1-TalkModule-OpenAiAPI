# Branch `h2-testing` — dashboard e voce per Unitree H2

Questo ramo contiene lo **sviluppo G1 Talk Module adattato per il robot Unitree H2**
(dashboard `/dashboard/`, telecamera, teaching, visitor profiles, gesti voce, ecc.).

## Repo collegate

| Repo | Ruolo |
|------|--------|
| **G1-TalkModule-OpenAiAPI** (`h2-testing`) | Sorgente codice completa — sviluppo qui |
| **[unitree-h2-testing](https://github.com/gpittonMeko/unitree-h2-testing)** | Deploy lab H2, patch braccio/demo, documentazione Thor |

## Deploy in lab H2

**Non fare deploy manuale solo da questo clone.** Usa la repo lab:

```powershell
git clone https://github.com/gpittonMeko/unitree-h2-testing.git
cd unitree-h2-testing
python scripts/deploy_g1_talk_to_h2.py
```

Lo script clona/checkout il ramo **`h2-testing`** di questa repo e **aggiunge** le patch H2
(`.env`, `robot_actions.json`, adapter braccio) **senza rimuovere** il lavoro dashboard già presente.

## Dashboard H2

- URL: `https://192.168.123.163:8081/dashboard/`
- Tab **Imposta**: API Key opzionale (TTS soundboard offline)
- Voce STT/LLM/TTS: file `.env` sulla Jetson (`OPENAI_API_KEY`)

Vedi [docs/G1_TALK_DASHBOARD_H2.md](https://github.com/gpittonMeko/unitree-h2-testing/blob/master/docs/G1_TALK_DASHBOARD_H2.md) nella repo `unitree-h2-testing`.

## Regola team

- Sviluppo dashboard/voce H2 → commit su **`h2-testing`**
- Codice G1 stabile → merge verso **`main`** solo dopo review
- Patch braccio/demo H2 → repo **`unitree-h2-testing`**
