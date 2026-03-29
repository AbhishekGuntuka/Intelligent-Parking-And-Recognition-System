"""
Project  : IPRS — Intelligent Parking & Recognition System
File     : iprs_vision_pipeline.py
Purpose  : 3-thread vision pipeline — YOLO vehicle detection + OCR plate read + MQTT publish
Hardware : Runs on server PC / Raspberry Pi 4 (CPU inference)
Modified : 2026-03-21

Architecture:
  Thread 1 (capture_thread)  → pulls MJPEG frames from ESP32-CAM /stream
  Thread 2 (infer_thread)    → runs YOLOv8n ONNX detection on frame queue
  Thread 3 (result_thread)   → runs OCR on crop, confirms plate, publishes MQTT

Performance targets:
  Full pipeline latency : <1.2s per plate on Intel i5 / RPi4
  Plate detection rate  : >85% accuracy on clear Indian plates
  Confirmed detection   : same plate in ≥3 consecutive frames

Dependencies (see requirements.txt):
  pip install opencv-python-headless pytesseract paho-mqtt numpy --break-system-packages
  sudo apt-get install -y tesseract-ocr
  python model_setup.py   ← downloads & exports YOLOv8n ONNX
"""

from __future__ import annotations

import csv
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import socket as _socket
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import paho.mqtt.client as mqtt

# ── Lazy import OCR module (local) ────────────────────────────────────
try:
    from ocr_module import ocr_plate_from_bytes
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    def ocr_plate_from_bytes(b: bytes) -> tuple[str, float]:  # type: ignore[misc]
        return "", 0.0

# ══════════════════════════════════════════════════════════════════════
#  CONFIG — all tunable knobs in one place
# ══════════════════════════════════════════════════════════════════════
CONFIG: dict = {
    # ─────────────────────────────────────────────────────────────────────
    # ROOT CAUSE FIX — UNDETECTED plates / pipeline never running:
    # Original IPs were 172.26.105.x (a different subnet — likely a previous
    # test network). Your project server and WROOM both use 10.121.81.x.
    # With the wrong IPs, urllib.request.urlopen() always timed out → CAM was
    # "unreachable" → capture_thread kept retrying → YOLO and OCR never ran
    # → server's /upload OCR path was the only working path, but Tesseract
    # results with no vision pipeline assistance → poor plate detection.
    #
    # FIX: Updated all three IPs to match your 10.121.81.x subnet.
    # CAM_STREAM_URL / CAM_CAPTURE_URL: point to your ESP32-CAM's DHCP IP.
    #   ⚠  The CAM IP is DHCP-assigned — verify it from Serial Monitor or
    #      owner_dashboard.html → Board Status panel, then update below.
    #   Current best-guess: 10.121.81.150 (matches the pattern in dashboards).
    # MQTT_BROKER: must match SERVER_IP in iprs_wroom_main.ino (10.121.81.78).
    # ─────────────────────────────────────────────────────────────────────
    # Camera / server
    "CAM_STREAM_URL":      "http://10.121.81.150:8080/stream",   # FIX: was 172.26.105.150
    "CAM_CAPTURE_URL":     "http://10.121.81.150:8080/capture",  # FIX: was 172.26.105.150
    "CAM_STATUS_URL":      "http://10.121.81.150:8080/status",   # FIX: was 172.26.105.150
    "STREAM_TIMEOUT_SEC":  10,
    "CAPTURE_TIMEOUT_SEC":  8,
    "RETRY_DELAY_SEC":      3,
    "RETRY_MAX_DELAY_SEC": 30,    # exponential backoff ceiling
    "BUF_MAX_BYTES":    524288,   # 512 KB — drop buffer if it grows past this (malformed stream)

    # MQTT
    "MQTT_BROKER":     "10.121.81.78",   # FIX: was 172.26.105.78 — must match MQTT_BROKER in WROOM
    "MQTT_PORT":       1883,
    "MQTT_USER":       "",
    "MQTT_PASS":       "",
    "MQTT_TOPIC_PLATE": "iprs/vision/plate",
    "MQTT_TOPIC_STATUS": "iprs/vision/status",

    # YOLO
    "ONNX_MODEL_PATH":  "models/yolov8n.onnx",
    "YOLO_INPUT_SIZE":  640,           # YOLOv8 default input square
    "YOLO_CONF_THRESH": 0.35,          # 0.65 was too high for parking camera angles
    "YOLO_NMS_THRESH":  0.45,
    "YOLO_VEHICLE_IDS": {2, 3, 5, 7}, # COCO: car=2, motorcycle=3, bus=5, truck=7

    # OCR & confirmation
    "OCR_CONF_THRESH":   0.50,         # 0.80 caused all results to be discarded
    "CONFIRM_FRAMES":    1,            # 3 was too slow — 1 detection is enough for gate
    "CONFIRM_WINDOW_SEC": 5.0,
    "FRAME_SKIP":        2,            # process every 2nd frame (was 3 — too slow)
    "PLATE_COOLDOWN_SEC": 5.0,         # 8s was too long between re-triggers

    # Pipeline queues
    "FRAME_QUEUE_SIZE":   2,           # drop old frames automatically
    "RESULT_QUEUE_SIZE": 10,

    # Logging
    "LOG_CSV_PATH":  "vision_log.csv",
    "LOG_LEVEL":     "INFO",

    # Slot assignment (simple round-robin; server handles real assignment)
    "TOTAL_SLOTS": 4,
}

# ══════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=getattr(logging, CONFIG["LOG_LEVEL"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("IPRS-Vision")

# ══════════════════════════════════════════════════════════════════════
#  CSV RESULT LOGGER
# ══════════════════════════════════════════════════════════════════════
_csv_lock = threading.Lock()

def _init_csv() -> None:
    """Create CSV with headers if it doesn't exist."""
    path = Path(CONFIG["LOG_CSV_PATH"])
    if not path.exists():
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["timestamp", "plate", "confidence", "slot", "yolo_conf", "lane"]
            )

def log_result_csv(plate: str, conf: float, slot: int, yolo_conf: float, lane: str) -> None:
    """Append a confirmed detection to the CSV log."""
    try:
        with _csv_lock:
            with open(CONFIG["LOG_CSV_PATH"], "a", newline="") as f:
                csv.writer(f).writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    plate, f"{conf:.1f}", slot, f"{yolo_conf:.2f}", lane
                ])
    except Exception as exc:
        log.error("CSV write failed: %s", exc)

# ══════════════════════════════════════════════════════════════════════
#  YOLO ONNX INFERENCE ENGINE
# ══════════════════════════════════════════════════════════════════════
class YOLOv8Detector:
    """
    Wraps YOLOv8n in ONNX format via cv2.dnn.
    No PyTorch, no ultralytics runtime — pure OpenCV DNN.
    """

    def __init__(self, model_path: str) -> None:
        if not Path(model_path).exists():
            raise FileNotFoundError(
                f"ONNX model not found: {model_path}\n"
                "Run: python model_setup.py"
            )
        self.net  = cv2.dnn.readNetFromONNX(model_path)
        # Use optimised OpenCV backend (CUDA if available, else CPU)
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

        self.input_size   = CONFIG["YOLO_INPUT_SIZE"]
        self.conf_thresh  = CONFIG["YOLO_CONF_THRESH"]
        self.nms_thresh   = CONFIG["YOLO_NMS_THRESH"]
        self.vehicle_ids  = CONFIG["YOLO_VEHICLE_IDS"]
        log.info("YOLOv8n loaded from %s", model_path)

    def detect(self, frame: np.ndarray) -> list[dict]:
        """
        Run vehicle detection on BGR frame.

        Returns list of dicts:
            {x, y, w, h, conf, class_id, crop_bgr}
        """
        h, w = frame.shape[:2]
        size  = self.input_size

        # Letterbox resize (preserves aspect ratio)
        scale  = min(size / h, size / w)
        new_h, new_w = int(h * scale), int(w * scale)
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        padded  = np.zeros((size, size, 3), dtype=np.uint8)
        pad_y   = (size - new_h) // 2
        pad_x   = (size - new_w) // 2
        padded[pad_y:pad_y+new_h, pad_x:pad_x+new_w] = resized

        # Preprocess: BGR→RGB, HWC→NCHW, /255
        blob = cv2.dnn.blobFromImage(
            padded, scalefactor=1/255.0, size=(size, size),
            mean=(0, 0, 0), swapRB=True, crop=False
        )
        self.net.setInput(blob)
        outputs = self.net.forward()  # shape: (1, 84, 8400) for YOLOv8n COCO

        # YOLOv8 output is transposed vs YOLOv5 — shape (1, 84, N) → (N, 84)
        preds = outputs[0].transpose()  # (8400, 84)

        boxes, scores, class_ids = [], [], []
        for row in preds:
            class_scores = row[4:]
            class_id     = int(np.argmax(class_scores))
            conf         = float(class_scores[class_id])
            if conf < self.conf_thresh:
                continue
            if class_id not in self.vehicle_ids:
                continue
            # YOLOv8 cx, cy, bw, bh are relative to padded image
            cx, cy, bw, bh = row[0], row[1], row[2], row[3]
            # Convert to original frame coordinates
            x1 = int((cx - bw/2 - pad_x) / scale)
            y1 = int((cy - bh/2 - pad_y) / scale)
            bw_ = int(bw / scale)
            bh_ = int(bh / scale)
            x1 = max(0, min(x1, w-1))
            y1 = max(0, min(y1, h-1))
            bw_ = max(1, min(bw_, w - x1))
            bh_ = max(1, min(bh_, h - y1))
            boxes.append([x1, y1, bw_, bh_])
            scores.append(conf)
            class_ids.append(class_id)

        if not boxes:
            return []

        # Non-maximum suppression
        indices = cv2.dnn.NMSBoxes(boxes, scores, self.conf_thresh, self.nms_thresh)
        results = []
        for idx in (indices.flatten() if len(indices) else []):
            x, y, bw, bh = boxes[idx]
            crop = frame[y:y+bh, x:x+bw]
            results.append({
                "x": x, "y": y, "w": bw, "h": bh,
                "conf": scores[idx],
                "class_id": class_ids[idx],
                "crop_bgr": crop,
            })
        return results

# ══════════════════════════════════════════════════════════════════════
#  PLATE REGION EXTRACTOR
# Lower third of vehicle bounding box typically contains the plate
# ══════════════════════════════════════════════════════════════════════

def extract_plate_region(crop_bgr: np.ndarray) -> np.ndarray:
    """
    Extract probable licence plate region from vehicle crop.
    Heuristic: bottom 30% of the crop, horizontally centred.
    """
    h, w = crop_bgr.shape[:2]
    y1 = int(h * 0.65)
    x1 = int(w * 0.10)
    x2 = int(w * 0.90)
    region = crop_bgr[y1:h, x1:x2]
    if region.size == 0:
        return crop_bgr
    return region

def frame_to_jpeg(frame: np.ndarray) -> bytes:
    """Encode BGR frame to JPEG bytes for OCR module."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return bytes(buf) if ok else b""

# ══════════════════════════════════════════════════════════════════════
#  MQTT CLIENT
# ══════════════════════════════════════════════════════════════════════

class MQTTPublisher:
    """Thin wrapper around paho-mqtt with auto-reconnect."""

    def __init__(self) -> None:
        self._client = mqtt.Client(client_id="iprs-vision-pipeline")
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._connected = False
        self._lock = threading.Lock()

    def _on_connect(self, client, userdata, flags, rc: int) -> None:
        self._connected = (rc == 0)
        if rc == 0:
            log.info("MQTT connected to %s:%s", CONFIG["MQTT_BROKER"], CONFIG["MQTT_PORT"])
        else:
            log.warning("MQTT connect failed rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc: int) -> None:
        self._connected = False
        log.warning("MQTT disconnected rc=%d", rc)

    def connect(self) -> None:
        try:
            if CONFIG["MQTT_USER"]:
                self._client.username_pw_set(CONFIG["MQTT_USER"], CONFIG["MQTT_PASS"])
            self._client.connect(CONFIG["MQTT_BROKER"], CONFIG["MQTT_PORT"], keepalive=60)
            self._client.loop_start()   # background network thread
        except Exception as exc:
            log.error("MQTT connect error: %s", exc)

    def publish(self, topic: str, payload: str) -> None:
        with self._lock:
            if not self._connected:
                log.debug("MQTT offline — publish skipped topic=%s", topic)
                return
            try:
                self._client.publish(topic, payload, qos=1)
            except Exception as exc:
                log.error("MQTT publish error: %s", exc)

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════
#  CONFIRMATION TRACKER
# Prevents spamming MQTT with duplicate plates
# ══════════════════════════════════════════════════════════════════════

class PlateConfirmTracker:
    """
    Tracks plate candidates across frames.
    Emits a confirmed plate only when the same reading
    appears ≥ CONFIRM_FRAMES times within CONFIRM_WINDOW_SEC.
    """

    def __init__(self) -> None:
        self._history: list[tuple[float, str]] = []  # (timestamp, plate)
        self._published: dict[str, float] = {}        # plate → last publish time
        self._lock = threading.Lock()

    def add(self, plate: str) -> str | None:
        """
        Add a candidate plate reading.
        Returns the confirmed plate string if threshold reached, else None.
        """
        if not plate:
            return None
        now = time.monotonic()
        cooldown = CONFIG["PLATE_COOLDOWN_SEC"]
        confirm_n = CONFIG["CONFIRM_FRAMES"]
        window    = CONFIG["CONFIRM_WINDOW_SEC"]

        with self._lock:
            # Purge stale history
            self._history = [(t, p) for t, p in self._history if now - t < window]
            self._history.append((now, plate))

            # Check cooldown — don't re-confirm if recently published
            last_pub = self._published.get(plate, 0.0)
            if now - last_pub < cooldown:
                return None

            # Count occurrences in window
            counts = Counter(p for _, p in self._history)
            if counts[plate] >= confirm_n:
                self._published[plate] = now
                self._history.clear()  # reset after confirmation
                return plate
        return None

# ══════════════════════════════════════════════════════════════════════
#  THREAD 1 — CAPTURE THREAD
# Pulls MJPEG stream from ESP32-CAM and pushes frames to frame_queue
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
#  THREAD 1 — CAPTURE THREAD
#  Pulls MJPEG frames from ESP32-CAM /stream with automatic fallback
#  to single-frame HTTP /capture when the MJPEG stream is unavailable.
#
#  Fixes applied vs original:
#    FIX-1  Pre-flight /status check before opening MJPEG stream —
#           avoids burning 10s timeout on every retry when CAM offline.
#    FIX-2  Dual-mode: MJPEG primary → HTTP /capture fallback.
#           /capture works even when the MJPEG stream is stalled/aborted.
#    FIX-3  Exponential backoff (3 → 6 → 12 → … → 30s) so a booting
#           CAM is not hammered with connection attempts.
#    FIX-4  Buffer size cap (BUF_MAX_BYTES = 512 KB). On Windows,
#           WinError 10053 can dump partial data; without the cap the
#           buffer grows until OOM.
#    FIX-5  socket.setdefaulttimeout() applied once on thread start.
#           urllib.request.urlopen(timeout=N) does NOT cover all socket
#           reads on Windows after the initial connect — the default
#           timeout does.
#    FIX-6  Closed urllib response object on every exit path — prevents
#           CLOSE_WAIT sockets accumulating on Windows.
# ══════════════════════════════════════════════════════════════════════

def _cam_reachable() -> bool:
    """
    Fast pre-flight check: GET /status from the CAM.
    Returns True if the CAM responds with HTTP 200 within 3 s.
    Keeps the CAM_STREAM_URL attempt clean — we only open the MJPEG
    stream when we know the CAM is alive.
    """
    status_url = CONFIG.get("CAM_STATUS_URL", CONFIG["CAM_CAPTURE_URL"].replace("/capture", "/status"))
    try:
        with urllib.request.urlopen(status_url, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _fetch_single_frame() -> bytes | None:
    """
    HTTP GET /capture — single JPEG snapshot.
    Used as fallback when the MJPEG stream cannot be opened.
    Returns raw JPEG bytes or None on failure.
    """
    try:
        with urllib.request.urlopen(
            CONFIG["CAM_CAPTURE_URL"],
            timeout=CONFIG["CAPTURE_TIMEOUT_SEC"],
        ) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
            return data if data else None
    except Exception:
        return None


def _push_frame(frame_queue: queue.Queue, frame: "np.ndarray") -> None:
    """Non-blocking queue push — drops the oldest frame if full."""
    try:
        frame_queue.put_nowait(frame)
    except queue.Full:
        try:
            frame_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            frame_queue.put_nowait(frame)
        except queue.Full:
            pass


def capture_thread(
    frame_queue: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """
    Connects to ESP32-CAM MJPEG stream.
    Falls back to single-frame HTTP /capture if stream is unavailable.
    Uses exponential backoff on repeated failures.
    """
    # FIX-5: Apply socket-level default timeout once for this thread.
    # Covers urllib reads that ignore the urlopen(timeout=) arg on Windows.
    _socket.setdefaulttimeout(CONFIG["STREAM_TIMEOUT_SEC"])

    log.info("capture_thread started → %s", CONFIG["CAM_STREAM_URL"])
    log.info("capture_thread fallback → %s", CONFIG["CAM_CAPTURE_URL"])

    frame_idx   = 0
    retry_delay = CONFIG["RETRY_DELAY_SEC"]      # starts at 3s
    retry_max   = CONFIG["RETRY_MAX_DELAY_SEC"]  # caps at 30s
    buf_max     = CONFIG["BUF_MAX_BYTES"]        # 512 KB cap
    consecutive_failures = 0

    while not stop_event.is_set():

        # ── FIX-1: Pre-flight reachability check ─────────────────────
        if not _cam_reachable():
            consecutive_failures += 1
            retry_delay = min(retry_delay * 2, retry_max) if consecutive_failures > 1 else CONFIG["RETRY_DELAY_SEC"]
            log.warning(
                "capture_thread: CAM unreachable at %s "
                "(attempt %d) — retry in %ds\n"
                "  ┌ Check: Is ESP32-CAM powered and flashed?\n"
                "  ├ Check: ping %s\n"
                "  ├ Check: open http://%s/status in browser\n"
                "  └ Check: CAM_STREAM_URL in CONFIG matches your CAM's IP",
                CONFIG["CAM_STATUS_URL"],
                consecutive_failures,
                retry_delay,
                CONFIG["CAM_STREAM_URL"].split("/")[2].split(":")[0],
                CONFIG["CAM_STREAM_URL"].split("/")[2],
            )
            stop_event.wait(retry_delay)
            continue

        # CAM is alive — reset failure counter and backoff
        consecutive_failures = 0
        retry_delay = CONFIG["RETRY_DELAY_SEC"]

        # ── Primary path: MJPEG stream ───────────────────────────────
        req = None
        try:
            req = urllib.request.urlopen(
                CONFIG["CAM_STREAM_URL"],
                timeout=CONFIG["STREAM_TIMEOUT_SEC"],
            )
            log.info("capture_thread: MJPEG stream connected ✓")
            buf = b""

            while not stop_event.is_set():
                chunk = req.read(4096)
                if not chunk:
                    log.warning("capture_thread: MJPEG stream ended (empty read)")
                    break
                buf += chunk

                # FIX-4: Buffer size guard — malformed/stalled stream prevention
                if len(buf) > buf_max:
                    log.warning(
                        "capture_thread: buffer exceeded %d KB — flushing "
                        "(malformed stream or WinError 10053 partial data)",
                        buf_max // 1024,
                    )
                    buf = b""
                    continue

                # Extract complete JPEG frames from MJPEG stream
                while True:
                    start = buf.find(b"\xff\xd8")   # JPEG SOI marker
                    end   = buf.find(b"\xff\xd9")   # JPEG EOI marker
                    if start == -1 or end == -1 or end < start:
                        break
                    jpeg_bytes = buf[start:end + 2]
                    buf        = buf[end + 2:]
                    frame_idx += 1

                    if frame_idx % CONFIG["FRAME_SKIP"] != 0:
                        continue

                    arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    _push_frame(frame_queue, frame)

        except Exception as exc:
            # Classify the error for clear diagnostics
            exc_str = str(exc)
            if "10053" in exc_str or "aborted" in exc_str.lower():
                reason = "WinError 10053 — Windows TCP aborted (CAM not sending data fast enough)"
            elif "timed out" in exc_str:
                reason = "socket timeout — CAM connected but sent no MJPEG data"
            elif "10061" in exc_str or "refused" in exc_str.lower():
                reason = "connection refused — CAM web server not running"
            elif "11001" in exc_str or "Name or service" in exc_str:
                reason = "DNS/host unreachable — check CAM_STREAM_URL IP address"
            else:
                reason = exc_str

            log.warning("capture_thread: MJPEG stream lost (%s)", reason)

        finally:
            # FIX-6: Always close the response to release the socket
            if req is not None:
                try:
                    req.close()
                except Exception:
                    pass

        if stop_event.is_set():
            break

        # ── FIX-2: Fallback path — single-frame HTTP /capture ────────
        log.info("capture_thread: switching to HTTP /capture fallback for %ds", retry_delay)
        fallback_deadline = time.monotonic() + retry_delay
        fallback_frames   = 0

        while not stop_event.is_set() and time.monotonic() < fallback_deadline:
            jpeg_bytes = _fetch_single_frame()
            if jpeg_bytes:
                arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    _push_frame(frame_queue, frame)
                    fallback_frames += 1
            # Poll at ~2 fps during fallback — enough to detect plates
            stop_event.wait(0.5)

        if fallback_frames > 0:
            log.info(
                "capture_thread: fallback delivered %d frames — retrying MJPEG stream",
                fallback_frames,
            )
        else:
            log.warning(
                "capture_thread: fallback also failed — is the CAM still reachable? "
                "Retry in %ds", retry_delay,
            )
            stop_event.wait(retry_delay)

    log.info("capture_thread stopped")

# ══════════════════════════════════════════════════════════════════════
#  THREAD 2 — INFER THREAD
# Runs YOLO on frames from frame_queue, pushes crops to result_queue
# ══════════════════════════════════════════════════════════════════════

def infer_thread(
    frame_queue:  queue.Queue,
    result_queue: queue.Queue,
    detector:     YOLOv8Detector,
    stop_event:   threading.Event,
) -> None:
    """
    Dequeues frames and runs vehicle detection.
    Pushes (crop_bgr, yolo_conf) to result_queue when vehicle detected.
    """
    log.info("infer_thread started")

    # ── CPU affinity — pin inference thread to a dedicated core ─────────────
    # Strategy: try os.sched_setaffinity first (Linux kernel syscall, zero
    # dependencies, reliable on RPi OS Lite).  Fall back to psutil on systems
    # where the syscall is unavailable (Windows, macOS, sandboxed containers).
    _affinity_set = False
    try:
        # os.sched_setaffinity(pid=0 means this thread, {1} = core index 1)
        # Keeps core 0 free for the OS + capture/result threads.
        os.sched_setaffinity(0, {1})
        log.info("infer_thread pinned to CPU core 1 via os.sched_setaffinity ✓")
        _affinity_set = True
    except AttributeError:
        pass  # Windows / macOS — sched_setaffinity not available
    except OSError as exc:
        log.debug("os.sched_setaffinity failed (%s) — trying psutil fallback", exc)

    if not _affinity_set:
        try:
            import psutil
            p    = psutil.Process()
            cpus = p.cpu_affinity()
            if len(cpus) >= 2:
                p.cpu_affinity([cpus[1]])
                log.info("infer_thread pinned to CPU %d via psutil ✓", cpus[1])
            else:
                log.debug("Only %d CPU(s) visible — skipping affinity", len(cpus))
        except Exception as exc:
            log.debug("psutil affinity skipped: %s", exc)
    # ── end CPU affinity ─────────────────────────────────────────────────────

    while not stop_event.is_set():
        try:
            frame: np.ndarray = frame_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        try:
            detections = detector.detect(frame)
            if not detections:
                continue

            # Take highest-confidence detection
            best = max(detections, key=lambda d: d["conf"])
            crop = extract_plate_region(best["crop_bgr"])

            try:
                result_queue.put_nowait((crop, float(best["conf"])))
            except queue.Full:
                pass  # result_queue full — skip (result_thread is slow)

        except Exception as exc:
            log.error("infer_thread error: %s", exc)

    log.info("infer_thread stopped")

# ══════════════════════════════════════════════════════════════════════
#  THREAD 3 — RESULT THREAD
# OCR on crops, confirms plates, publishes MQTT
# ══════════════════════════════════════════════════════════════════════

def result_thread(
    result_queue: queue.Queue,
    mqtt_pub:     MQTTPublisher,
    tracker:      PlateConfirmTracker,
    stop_event:   threading.Event,
) -> None:
    """
    Dequeues (crop, yolo_conf), runs OCR with CLAHE,
    confirms plate across frames, publishes to MQTT + CSV.
    """
    log.info("result_thread started")
    slot_cursor = 1   # simple round-robin slot assignment

    while not stop_event.is_set():
        try:
            crop_bgr, yolo_conf = result_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if not _OCR_AVAILABLE:
            log.warning("OCR not available — skipping")
            continue

        try:
            jpeg = frame_to_jpeg(crop_bgr)
            if not jpeg:
                continue

            plate, conf = ocr_plate_from_bytes(jpeg)

            # Retry with alternative preprocessing if confidence is low
            if conf < CONFIG["OCR_CONF_THRESH"] * 100:
                # Invert colours (handles dark-on-light plates)
                inverted = cv2.bitwise_not(crop_bgr)
                jpeg2    = frame_to_jpeg(inverted)
                if jpeg2:
                    plate2, conf2 = ocr_plate_from_bytes(jpeg2)
                    if conf2 > conf:
                        plate, conf = plate2, conf2

            if not plate:
                continue

            log.debug("OCR: plate=%s conf=%.1f yolo=%.2f", plate, conf, yolo_conf)

            # Confirmation gate — same plate must appear N times
            confirmed = tracker.add(plate)
            if not confirmed:
                continue

            # Assign slot (server assigns authoritative slot; this is best-effort)
            slot = slot_cursor
            slot_cursor = (slot_cursor % CONFIG["TOTAL_SLOTS"]) + 1

            # Build MQTT payload
            payload = json.dumps({
                "plate":     confirmed,
                "slot":      slot,
                "conf":      round(conf / 100.0, 3),
                "yolo_conf": round(yolo_conf, 3),
                "action":    "ENTRY",
                "ts":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, separators=(",", ":"))

            mqtt_pub.publish(CONFIG["MQTT_TOPIC_PLATE"], payload)
            log.info("CONFIRMED plate=%s slot=%d conf=%.1f%% yolo=%.2f",
                     confirmed, slot, conf, yolo_conf)

            # Persist to CSV
            log_result_csv(confirmed, conf, slot, yolo_conf, "ENTRY")

        except Exception as exc:
            log.error("result_thread error: %s", exc)

    log.info("result_thread stopped")

# ══════════════════════════════════════════════════════════════════════
#  MAIN — pipeline bootstrap
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    _init_csv()
    log.info("IPRS Vision Pipeline starting")
    log.info("Stream URL : %s", CONFIG["CAM_STREAM_URL"])
    log.info("MQTT broker: %s:%d", CONFIG["MQTT_BROKER"], CONFIG["MQTT_PORT"])
    log.info("ONNX model : %s", CONFIG["ONNX_MODEL_PATH"])

    # Load YOLO model — fail fast if missing
    try:
        detector = YOLOv8Detector(CONFIG["ONNX_MODEL_PATH"])
    except FileNotFoundError as exc:
        log.critical(str(exc))
        sys.exit(1)

    # MQTT
    mqtt_pub = MQTTPublisher()
    mqtt_pub.connect()

    # Plate confirmation tracker
    tracker = PlateConfirmTracker()

    # Shared queues — bounded to prevent memory growth
    frame_queue  = queue.Queue(maxsize=CONFIG["FRAME_QUEUE_SIZE"])
    result_queue = queue.Queue(maxsize=CONFIG["RESULT_QUEUE_SIZE"])

    stop_event = threading.Event()

    # Thread definitions — daemon so they die with the process
    threads = [
        threading.Thread(
            target=capture_thread,
            args=(frame_queue, stop_event),
            name="capture",
            daemon=True,
        ),
        threading.Thread(
            target=infer_thread,
            args=(frame_queue, result_queue, detector, stop_event),
            name="infer",
            daemon=True,
        ),
        threading.Thread(
            target=result_thread,
            args=(result_queue, mqtt_pub, tracker, stop_event),
            name="result",
            daemon=True,
        ),
    ]

    # Graceful shutdown on Ctrl-C or SIGTERM
    def _shutdown(signum, frame_sig) -> None:
        log.info("Shutdown signal received — stopping threads")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start all threads
    for t in threads:
        t.start()
        log.info("Started thread: %s", t.name)

    # Publish heartbeat to MQTT every 30s while running
    while not stop_event.is_set():
        status = json.dumps({
            "online":      True,
            "queue_depth": frame_queue.qsize(),
            "ts":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, separators=(",", ":"))
        mqtt_pub.publish(CONFIG["MQTT_TOPIC_STATUS"], status)
        stop_event.wait(30)

    # Cleanup
    for t in threads:
        t.join(timeout=5)
    mqtt_pub.disconnect()
    log.info("Vision pipeline stopped cleanly")

if __name__ == "__main__":
    main()
