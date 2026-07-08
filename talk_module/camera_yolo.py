"""Camera integrata G1 (V4L2 / RealSense) con overlay YOLO per stream MJPEG."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

import numpy as np

_lock = threading.Lock()
_service: Optional["CameraYoloService"] = None


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


class CameraYoloService:
    def __init__(self) -> None:
        self.source = (os.getenv("G1_CAMERA_SOURCE") or "v4l").strip().lower()
        self.device = (os.getenv("G1_CAMERA_DEVICE") or "0").strip()
        self.model_name = (os.getenv("G1_YOLO_MODEL") or "yolov8n.onnx").strip()
        self.yolo_backend = (os.getenv("G1_YOLO_BACKEND") or "onnx").strip().lower()
        self.width = _env_int("G1_CAMERA_WIDTH", 640)
        self.height = _env_int("G1_CAMERA_HEIGHT", 480)
        self.fps = _env_int("G1_CAMERA_FPS", 15)
        self.conf = _env_float("G1_YOLO_CONF", 0.35)
        self.yolo_enabled = (os.getenv("G1_CAMERA_YOLO", "1") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        self.depth_enabled = (os.getenv("G1_CAMERA_DEPTH", "1") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        raw_classes = (os.getenv("G1_YOLO_CLASSES") or "").strip()
        self.yolo_classes: Optional[set[str]] = (
            {c.strip().lower() for c in raw_classes.split(",") if c.strip()} if raw_classes else None
        )

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._backend: Any = None
        self._backend_kind = ""
        self._align: Any = None
        self._yolo: Any = None
        self._yolo_backend_loaded = ""
        self._yolo_error: Optional[str] = None
        self._open_error: Optional[str] = None
        self._latest_jpeg: Optional[bytes] = None
        self._latest_ts = 0.0
        self._frame_count = 0
        self._detections: list[dict[str, Any]] = []
        self._fps_measured = 0.0

    def status(self) -> dict[str, Any]:
        with _lock:
            return {
                "running": self._running,
                "source": self.source,
                "device": self.device,
                "backend": self._backend_kind or None,
                "yolo_enabled": self.yolo_enabled,
                "yolo_model": self.model_name if self.yolo_enabled else None,
                "yolo_backend": self.yolo_backend if self.yolo_enabled else None,
                "yolo_loaded": self._yolo is not None,
                "yolo_backend_loaded": self._yolo_backend_loaded or None,
                "yolo_error": self._yolo_error,
                "depth_enabled": self.depth_enabled and self.source == "realsense",
                "yolo_classes": sorted(self.yolo_classes) if self.yolo_classes else None,
                "open_error": self._open_error,
                "frame_count": self._frame_count,
                "fps": round(self._fps_measured, 1),
                "resolution": f"{self.width}x{self.height}",
                "detections": list(self._detections),
                "has_frame": self._latest_jpeg is not None,
            }

    def start(self) -> None:
        with _lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, name="camera-yolo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with _lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close_backend()

    def get_jpeg(self) -> Optional[bytes]:
        with _lock:
            return self._latest_jpeg

    def _close_backend(self) -> None:
        backend = self._backend
        kind = self._backend_kind
        self._backend = None
        self._backend_kind = ""
        self._align = None
        if not backend:
            return
        try:
            if kind == "v4l":
                backend.release()
            elif kind == "realsense":
                backend.stop()
        except Exception:
            pass

    def _open_v4l(self) -> bool:
        try:
            import cv2  # type: ignore

            dev: Any = self.device
            if dev.isdigit():
                dev = int(dev)
            cap = cv2.VideoCapture(dev)
            if not cap.isOpened():
                cap.release()
                self._open_error = f"V4L2: impossibile aprire {self.device!r} (prova ls /dev/video*)"
                return False
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            self._backend = cap
            self._backend_kind = "v4l"
            print(f"[camera] V4L2 device={self.device!r}", flush=True)
            return True
        except Exception as e:
            self._open_error = f"OpenCV: {e}"
            print(f"[camera] {self._open_error}", flush=True)
            return False

    def _open_realsense(self) -> bool:
        try:
            import pyrealsense2 as rs  # type: ignore

            pipeline = rs.pipeline()
            config = rs.config()
            config.enable_stream(
                rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps
            )
            use_depth = self.depth_enabled
            if use_depth:
                config.enable_stream(
                    rs.stream.depth, self.width, self.height, rs.format.z16, self.fps
                )
            pipeline.start(config)
            self._backend = pipeline
            self._backend_kind = "realsense"
            self._align = rs.align(rs.stream.color) if use_depth else None
            print(
                f"[camera] RealSense avviata (depth={'on' if use_depth else 'off'})",
                flush=True,
            )
            return True
        except Exception as e:
            self._open_error = f"RealSense: {e}"
            print(f"[camera] RealSense non disponibile: {e}", flush=True)
            return False

    def _open_backend(self) -> bool:
        self._close_backend()
        self._open_error = None
        if self.source == "realsense":
            if self._open_realsense():
                return True
            print("[camera] RealSense fallita → provo V4L2 (webcam /dev/video0)", flush=True)
        return self._open_v4l()

    def _read_frame_bgr(self) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        if self._backend_kind == "realsense":
            try:
                frames = self._backend.wait_for_frames(timeout_ms=500)
                if self._align is not None:
                    frames = self._align.process(frames)
                color = frames.get_color_frame()
                if not color:
                    return None, None
                bgr = np.asanyarray(color.get_data())
                depth_mm: Optional[np.ndarray] = None
                if self._align is not None:
                    depth = frames.get_depth_frame()
                    if depth:
                        depth_mm = np.asanyarray(depth.get_data())
                return bgr, depth_mm
            except Exception:
                return None, None
        try:
            import cv2  # type: ignore

            ok, frame = self._backend.read()
            if not ok or frame is None:
                return None, None
            return frame, None
        except Exception:
            return None, None

    @staticmethod
    def _bbox_depth_m(depth_mm: np.ndarray, bbox: list[int]) -> Optional[float]:
        x, y, w, h = bbox
        h_img, w_img = depth_mm.shape[:2]
        x1 = max(0, min(w_img - 1, x))
        y1 = max(0, min(h_img - 1, y))
        x2 = max(x1 + 1, min(w_img, x + w))
        y2 = max(y1 + 1, min(h_img, y + h))
        roi = depth_mm[y1:y2, x1:x2]
        valid = roi[(roi > 0) & (roi < 12000)]
        if valid.size < 10:
            return None
        return round(float(np.median(valid)) / 1000.0, 2)

    @staticmethod
    def _overlay_depth(frame: np.ndarray, dets: list[dict[str, Any]]) -> np.ndarray:
        import cv2  # type: ignore

        for d in dets:
            dm = d.get("depth_m")
            bbox = d.get("bbox")
            if dm is None or not bbox:
                continue
            x, y, w, h = bbox
            y_txt = min(frame.shape[0] - 4, y + h + 18)
            cv2.putText(
                frame,
                f"{dm:.2f}m",
                (x, y_txt),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (80, 200, 255),
                2,
                cv2.LINE_AA,
            )
        return frame

    def _resolve_model_path(self) -> Path:
        from pathlib import Path

        name = self.model_name
        if name.endswith(".pt"):
            name = name[:-3] + ".onnx"
        p = Path(name)
        if p.is_file():
            return p
        return Path(__file__).resolve().parent.parent / "config" / "models" / Path(name).name

    def _ensure_yolo(self) -> None:
        if not self.yolo_enabled or self._yolo is not None or self._yolo_error:
            return
        if self.yolo_backend == "ultralytics":
            try:
                from ultralytics import YOLO  # type: ignore

                self._yolo = YOLO(self.model_name)
                self._yolo_backend_loaded = "ultralytics"
                print(f"[camera] YOLO ultralytics: {self.model_name}", flush=True)
            except Exception as e:
                self._yolo_error = str(e)
                print(f"[camera] ultralytics non disponibile: {e}", flush=True)
            return
        try:
            from talk_module.yolo_onnx import YoloOnnxDetector, ensure_onnx_model

            path = ensure_onnx_model(self._resolve_model_path())
            self._yolo = YoloOnnxDetector(path, conf=self.conf, class_filter=self.yolo_classes)
            self._yolo_backend_loaded = "onnx"
            print(f"[camera] YOLO ONNX: {path}", flush=True)
        except Exception as e:
            self._yolo_error = str(e)
            print(f"[camera] YOLO ONNX non disponibile: {e}", flush=True)

    def _annotate(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
        import cv2  # type: ignore

        self._ensure_yolo()
        if not self._yolo:
            return frame, []
        try:
            if self._yolo_backend_loaded == "onnx":
                return self._yolo.annotate(frame)
            results = self._yolo(frame, conf=self.conf, verbose=False)
            if not results:
                return frame, []
            r0 = results[0]
            annotated = r0.plot()
            dets: list[dict[str, Any]] = []
            names = r0.names or {}
            if r0.boxes is not None:
                for box in r0.boxes:
                    cls_id = int(box.cls[0]) if box.cls is not None else -1
                    conf = float(box.conf[0]) if box.conf is not None else 0.0
                    label = names.get(cls_id, str(cls_id))
                    dets.append({"class": label, "confidence": round(conf, 2)})
            return annotated, dets
        except Exception as e:
            self._yolo_error = str(e)
            cv2.putText(
                frame,
                f"YOLO err: {e}"[:60],
                (8, 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 80, 255),
                2,
                cv2.LINE_AA,
            )
            return frame, []

    def _loop(self) -> None:
        interval = 1.0 / max(self.fps, 1)
        fails = 0
        while True:
            with _lock:
                if not self._running:
                    break
            if self._backend is None:
                if not self._open_backend():
                    time.sleep(1.0)
                    continue
                fails = 0

            t0 = time.perf_counter()
            frame, depth_mm = self._read_frame_bgr()
            if frame is None:
                fails += 1
                if fails > 30:
                    self._close_backend()
                    fails = 0
                time.sleep(0.05)
                continue
            fails = 0

            annotated, dets = self._annotate(frame)
            if depth_mm is not None:
                for d in dets:
                    bbox = d.get("bbox")
                    if bbox:
                        dm = self._bbox_depth_m(depth_mm, bbox)
                        if dm is not None:
                            d["depth_m"] = dm
                if dets:
                    annotated = self._overlay_depth(annotated, dets)
            try:
                import cv2  # type: ignore

                ok, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ok:
                    time.sleep(interval)
                    continue
                jpeg = buf.tobytes()
            except Exception as e:
                self._open_error = f"encode: {e}"
                time.sleep(interval)
                continue

            elapsed = max(time.perf_counter() - t0, 0.001)
            with _lock:
                self._latest_jpeg = jpeg
                self._latest_ts = time.time()
                self._frame_count += 1
                self._detections = dets[:12]
                self._fps_measured = 0.85 * self._fps_measured + 0.15 * (1.0 / elapsed)

            try:
                from talk_module.pick_on_detect import get_pick_service

                get_pick_service().on_detections(dets[:12])
            except Exception as _pick_err:
                print(f"[camera] pick_on_detect: {_pick_err}", flush=True)

            sleep_s = max(0.0, interval - (time.perf_counter() - t0))
            time.sleep(sleep_s)

        self._close_backend()


def get_camera_service() -> CameraYoloService:
    global _service
    if _service is None:
        _service = CameraYoloService()
    return _service
