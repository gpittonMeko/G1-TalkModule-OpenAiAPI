"""Detector YOLOv8 via OpenCV DNN + ONNX (leggero, niente PyTorch/CUDA su Jetson)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np

# COCO 80 classi (YOLOv8 pretrain)
_COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)

_YOLOV8N_ONNX_URLS = (
    "https://huggingface.co/Kalray/yolov8/resolve/main/yolov8n.onnx",
    "https://huggingface.co/cabelo/yolov8/resolve/main/yolov8n.onnx",
    "https://huggingface.co/Xuban/yolo_weights_database/resolve/main/yolov8n.onnx",
)
_MIN_ONNX_BYTES = 5_000_000  # ~12.8 MB attesi; 9 byte = 404/HTML corrotto


def default_onnx_model_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "models" / "yolov8n.onnx"


def ensure_onnx_model(path: Path) -> Path:
    if path.exists() and path.stat().st_size >= _MIN_ONNX_BYTES:
        return path
    if path.exists() and path.stat().st_size < _MIN_ONNX_BYTES:
        print(f"[camera] Rimuovo ONNX corrotto ({path.stat().st_size} byte): {path}", flush=True)
        path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    import requests

    last_err: Optional[Exception] = None
    for url in _YOLOV8N_ONNX_URLS:
        try:
            print(f"[camera] Download YOLO ONNX da {url}", flush=True)
            with requests.get(url, stream=True, timeout=180, allow_redirects=True) as r:
                r.raise_for_status()
                tmp = path.with_suffix(path.suffix + ".part")
                nbytes = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
                            nbytes += len(chunk)
                if nbytes < _MIN_ONNX_BYTES:
                    tmp.unlink(missing_ok=True)
                    raise ValueError(f"file troppo piccolo ({nbytes} byte) — URL non valido")
                tmp.replace(path)
            print(f"[camera] YOLO ONNX ok ({path.stat().st_size // (1 << 20)} MB)", flush=True)
            return path
        except Exception as e:
            last_err = e
            print(f"[camera] Download fallito: {e}", flush=True)
    raise RuntimeError(f"Impossibile scaricare yolov8n.onnx: {last_err}")


class YoloOnnxDetector:
    """YOLOv8n ONNX con cv2.dnn — CPU, adatto a Jetson senza pip torch."""

    def __init__(
        self,
        model_path: Path,
        conf: float = 0.35,
        input_size: int = 640,
        class_filter: Optional[set[str]] = None,
    ) -> None:
        import cv2  # type: ignore

        self.conf = conf
        self.input_size = input_size
        self.class_filter = {c.strip().lower() for c in class_filter} if class_filter else None
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

    def annotate(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
        import cv2  # type: ignore

        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            frame, 1.0 / 255.0, (self.input_size, self.input_size), swapRB=True, crop=False
        )
        self.net.setInput(blob)
        out = self.net.forward()
        if out.ndim == 3:
            preds = np.squeeze(out, axis=0).T
        else:
            preds = np.squeeze(out).T

        boxes: list[list[int]] = []
        scores: list[float] = []
        class_ids: list[int] = []
        x_factor = w / self.input_size
        y_factor = h / self.input_size

        for row in preds:
            cls_scores = row[4:]
            cls_id = int(np.argmax(cls_scores))
            score = float(cls_scores[cls_id])
            if score < self.conf:
                continue
            cx, cy, bw, bh = row[0], row[1], row[2], row[3]
            x1 = int((cx - bw / 2) * x_factor)
            y1 = int((cy - bh / 2) * y_factor)
            bw_i = int(bw * x_factor)
            bh_i = int(bh * y_factor)
            boxes.append([x1, y1, bw_i, bh_i])
            scores.append(score)
            class_ids.append(cls_id)

        dets: list[dict[str, Any]] = []
        if boxes:
            idxs = cv2.dnn.NMSBoxes(boxes, scores, self.conf, 0.45)
            if len(idxs) > 0:
                for i in np.array(idxs).flatten()[:12]:
                    x, y, bw_i, bh_i = boxes[i]
                    label = _COCO_NAMES[class_ids[i]] if class_ids[i] < len(_COCO_NAMES) else str(class_ids[i])
                    if self.class_filter and label.lower() not in self.class_filter:
                        continue
                    conf = round(scores[i], 2)
                    dets.append(
                        {
                            "class": label,
                            "confidence": conf,
                            "bbox": [int(x), int(y), int(bw_i), int(bh_i)],
                        }
                    )
                    label_txt = f"{label} {conf:.2f}"
                    cv2.rectangle(frame, (x, y), (x + bw_i, y + bh_i), (0, 220, 120), 2)
                    cv2.putText(
                        frame,
                        label_txt,
                        (x, max(16, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 220, 120),
                        1,
                        cv2.LINE_AA,
                    )
        return frame, dets
