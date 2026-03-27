# Azioni robot G1 – Comandi vocali

Comandi come "dare la mano" o "saluta" vengono instradati al robot G1 tramite Unitree SDK.

## Configurazione

### config/robot_actions.json

```json
{
  "dare la mano": {"action": "shake_hand", "response": "Ecco la mano!"},
  "saluta": {"action": "wave_hand", "response": "Ciao!"}
}
```

- **pattern**: frase vocale (match per substring)
- **action**: ID azione (shake_hand, wave_hand, teaching_X)
- **response**: risposta TTS

### .env

```env
UNITREE_ROBOT_IP=192.168.123.161
```

## Azioni supportate

| action_id | Descrizione |
|-----------|-------------|
| shake_hand | Stringi la mano |
| wave_hand | Saluta con la mano |
| teaching_X | Azione teaching (X = ID dalla app Unitree) |

## Prerequisiti

1. **Robot in sport mode**: L1+A sul telecomando (non debug mode)
2. **Firmware**: v1.3.0+ per ShakeHand/WaveHand
3. **SDK sul Jetson** (braccia + locomozione `LocoClient`): non è un semplice `pip install` su aarch64. Usa lo script e la guida **[JETSON_UNITREE_SDK.md](JETSON_UNITREE_SDK.md)** (`scripts/install_unitree_sdk_jetson.sh`).

## Script personalizzato

Per azioni custom o teaching, crea `scripts/robot_action.sh`:

```bash
#!/bin/bash
# $1 = action_id, $2 = robot_ip
action="$1"
ip="$2"
# La tua logica (HTTP, SDK, ecc.)
exit 0
```

Rendi eseguibile: `chmod +x scripts/robot_action.sh`

## Teaching (azioni registrate)

Le azioni registrate nell’app Unitree (Demo Teaching) hanno un ID. Aggiungi in robot_actions.json:

```json
"fai il saluto": {"action": "teaching_1", "response": "Ecco!"}
```

Sostituisci `teaching_1` con l’ID reale dalla app. Per il playback via SDK serve la documentazione Unitree; altrimenti usa `robot_action.sh`.
