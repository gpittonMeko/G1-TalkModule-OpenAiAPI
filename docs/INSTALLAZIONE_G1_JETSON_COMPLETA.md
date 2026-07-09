# Installazione G1 Talk Module — Jetson completa (nuovo G1)

Guida per **prima installazione** sul computer di bordo del G1.  
`bash install.sh` da solo **non basta**: mancano **Unitree SDK** (movimenti) e **OpenCV** (visione dashboard).

---

## Cosa installa cosa

| Componente | Script / requirements | A cosa serve |
|------------|----------------------|--------------|
| Voce STT/LLM/TTS, web | `install.sh` → `requirements.txt` | Talk Module base |
| **OpenCV + YOLO** | `requirements-camera.txt` | Dashboard `/dashboard`, stream camera, rilevamento oggetti |
| **Unitree SDK2** | `scripts/install_unitree_sdk_jetson.sh` | Braccia, locomozione, audio robot via DDS |
| Modello YOLO | `scripts/download_yolov8n_onnx.sh` | `yolov8n.onnx` in `config/models/` |
| RealSense (opz.) | `scripts/install_realsense_jetson.sh` | Camera «occhi» integrata G1 |

---

## Installazione rapida (consigliata)

Sul Jetson del G1 (`unitree@<IP>`):

```bash
cd ~/G1-TalkModule-OpenAiAPI

# Una sola riga — base + OpenCV/YOLO + Unitree SDK
bash scripts/install_jetson_completo.sh
```

Con camera RealSense integrata (più lungo):

```bash
bash scripts/install_jetson_completo.sh --realsense
```

---

## Passi manuali (se preferisci)

```bash
cd ~/G1-TalkModule-OpenAiAPI

# 1) Base
bash scripts/install_jetson_tutto_in_cartella.sh

# 2) Visione (OpenCV)
pip install -r requirements-camera.txt
# oppure: pip install -r requirements-jetson.txt

bash scripts/download_yolov8n_onnx.sh

# 3) Movimenti robot (OBBLIGATORIO per braccia/loco)
bash scripts/install_unitree_sdk_jetson.sh

# 4) Config
cp .env.example .env
nano .env   # OPENAI_API_KEY, TALK_PUBLIC_HOST, UNITREE_DDS_INTERFACE=eth0

# 5) Certificato HTTPS
TALK_PUBLIC_HOST=192.168.123.164 bash scripts/generate_ssl_cert.sh

# 6) Avvio
bash scripts/restart_server.sh
```

---

## File `.env` minimi (Jetson G1)

```env
OPENAI_API_KEY=sk-...

TALK_PUBLIC_HOST=192.168.123.164
UNITREE_ROBOT_IP=192.168.123.161
UNITREE_DDS_INTERFACE=eth0

# Visione dashboard
G1_CAMERA_YOLO=1
G1_YOLO_BACKEND=onnx
G1_YOLO_MODEL=yolov8n.onnx
```

Per RealSense: `G1_CAMERA_SOURCE=realsense` (dopo `install_realsense_jetson.sh`).

---

## Verifica

```bash
.venv/bin/python3 scripts/verify_jetson_deps.py
bash scripts/diagnose_g1_robot.py
curl -k https://127.0.0.1:8081/api/health
```

| Check | OK se |
|-------|--------|
| `opencv (cv2)` | import senza errori |
| `unitree_sdk2py LocoClient` | SDK installato |
| `YOLO ONNX model` | file `config/models/yolov8n.onnx` presente |
| `diagnose_g1_robot.py` | DDS raggiungibile, robot in sport mode |

---

## Pacchetti che spesso mancano su G1 nuovo

| Manca | Sintomo | Fix |
|-------|---------|-----|
| **unitree_sdk2py** | «Installa unitree_sdk2_python», braccia ferme | `bash scripts/install_unitree_sdk_jetson.sh` |
| **opencv (cv2)** | Dashboard camera nera, errore import cv2 | `pip install -r requirements-camera.txt` |
| **yolov8n.onnx** | YOLO non parte | `bash scripts/download_yolov8n_onnx.sh` |
| **Cyclone DDS** | LocoClient import fail | incluso in `install_unitree_sdk_jetson.sh` |
| **pyrealsense2** | Solo se usi camera RealSense | `bash scripts/install_realsense_jetson.sh` |

**Non installare** `ultralytics` sul Jetson (scarica PyTorch ~500MB). Usa `G1_YOLO_BACKEND=onnx` (default).

---

## Requirements (riferimento)

| File | Contenuto |
|------|-----------|
| `requirements.txt` | Voce, web, API |
| `requirements-camera.txt` | `opencv-python-headless` |
| `requirements-jetson.txt` | Base + camera (unisce i due sopra) |

SDK Unitree: **non** su PyPI per aarch64 → script dedicato.

---

## Altri documenti

- [JETSON_UNITREE_SDK.md](JETSON_UNITREE_SDK.md) — dettaglio SDK/DDS
- [INSTALLAZIONE.md](INSTALLAZIONE.md) — guida generale
- [GUIDA.md](../GUIDA.md) — guida rapida
