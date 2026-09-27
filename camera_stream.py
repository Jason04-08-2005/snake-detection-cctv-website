"""
camera_stream.py
Two kinds of camera worker, sharing all detection/screenshot/alarm/DB logic
via _BaseCameraWorker:

  - CameraWorker        : "pulls" frames itself, in its own thread, from an
                           RTSP URL or a server-side webcam index. Used for
                           real CCTV cameras and the server's own webcam.

  - BrowserCameraWorker  : "receives" frames pushed to it from a browser
                           that granted camera permission (getUserMedia).
                           No thread of its own - frames arrive via
                           ingest_frame() from a Flask request handler.

Both expose the same get_latest_jpeg()/stop()/status interface, so the rest
of the app (the MJPEG endpoint, the dashboard) doesn't need to care which
kind of camera it's looking at.
"""

import os
import time
import threading
from datetime import datetime

import cv2
import numpy as np

import config
import database
import alarm


class _BaseCameraWorker:
    """Shared detection / screenshot / alarm / DB logic for any camera source."""

    def __init__(self, camera_id, name, detector):
        self.id = camera_id
        self.name = name
        self.detector = detector

        self._latest_frame = None
        self._frame_lock = threading.Lock()

        self._frame_count = 0
        self._last_detection_time = 0
        self.status = "starting"  # starting | online | reconnecting | offline

    def get_latest_jpeg(self):
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            ok, buf = cv2.imencode(".jpg", self._latest_frame)
            return buf.tobytes() if ok else None

    def _process_frame(self, frame):
        try:
            detections = self.detector.detect(frame)
        except Exception as e:
            print(f"[{self.name}] Detection error: {e}")
            return frame

        if detections:
            annotated = self.detector.draw_boxes(frame.copy(), detections)
            self._handle_detection(annotated, detections)
            return annotated

        return frame

    def _handle_detection(self, annotated_frame, detections):
        now = time.time()
        if now - self._last_detection_time < config.DETECTION_COOLDOWN_SECONDS:
            return  # still in cooldown, skip logging/alarm/screenshot spam
        self._last_detection_time = now

        best = max(detections, key=lambda d: d["confidence"])
        confidence = best["confidence"]

        image_path = None
        if config.SAVE_SCREENSHOT_ON_DETECTION:
            image_path = self._save_screenshot(annotated_frame)

        database.log_detection(self.id, self.name, confidence, image_path)
        database.update_camera_status(self.id, self.name, "online", detection=True)
        alarm.trigger_alarm(self.name, confidence)

    def _save_screenshot(self, frame):
        os.makedirs(config.SCREENSHOT_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{self.id}_{timestamp}.jpg"
        filepath = os.path.join(config.SCREENSHOT_DIR, filename)
        cv2.imwrite(filepath, frame)
        return f"screenshots/{filename}"  # relative path, served via Flask static


class CameraWorker(_BaseCameraWorker):
    """
    Pull-based worker: connects to an RTSP URL (or a server-side webcam
    index, for local testing) and reads frames itself in its own thread,
    with automatic reconnection if the stream drops.
    """

    def __init__(self, camera_cfg, detector):
        super().__init__(camera_cfg["id"], camera_cfg["name"], detector)
        self.rtsp_url = camera_cfg["rtsp_url"]

        self._running = False
        self._thread = None
        self._cap = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._cap:
            self._cap.release()

    def _connect(self):
        source = self.rtsp_url
        if isinstance(source, str) and source.isdigit():
            source = int(source)  # webcam index for local testing
        cap = cv2.VideoCapture(source)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimize latency/RAM buildup
        return cap

    def _run(self):
        database.update_camera_status(self.id, self.name, "starting")

        while self._running:
            self._cap = self._connect()

            if not self._cap or not self._cap.isOpened():
                self.status = "offline"
                database.update_camera_status(self.id, self.name, "offline")
                database.increment_reconnect(self.id)
                print(f"[{self.name}] Could not connect. Retrying in {config.RECONNECT_DELAY_SECONDS}s...")
                time.sleep(config.RECONNECT_DELAY_SECONDS)
                continue

            self.status = "online"
            database.update_camera_status(self.id, self.name, "online")
            print(f"[{self.name}] Connected.")

            while self._running:
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    print(f"[{self.name}] Stream dropped. Reconnecting...")
                    self.status = "reconnecting"
                    database.update_camera_status(self.id, self.name, "reconnecting")
                    database.increment_reconnect(self.id)
                    break

                frame = cv2.resize(frame, (config.FRAME_WIDTH, config.FRAME_HEIGHT))
                self._frame_count += 1

                display_frame = frame
                if self._frame_count % config.PROCESS_EVERY_N_FRAMES == 0:
                    display_frame = self._process_frame(frame)

                with self._frame_lock:
                    self._latest_frame = display_frame

                database.update_camera_status(self.id, self.name, "online")

            if self._cap:
                self._cap.release()
            if self._running:
                time.sleep(config.RECONNECT_DELAY_SECONDS)

        self.status = "offline"
        database.update_camera_status(self.id, self.name, "offline")


class BrowserCameraWorker(_BaseCameraWorker):
    """
    Push-based worker: a browser that granted camera permission
    (navigator.mediaDevices.getUserMedia) captures frames itself and POSTs
    each one as a JPEG to /api/cameras/<id>/frame. This class just decodes
    and processes whatever arrives - there is no capture thread, and
    "offline" here means no frame has arrived recently rather than a failed
    network connection.
    """

    STALE_AFTER_SECONDS = 8  # no frames in this long -> considered offline

    def __init__(self, camera_id, name, detector):
        super().__init__(camera_id, name, detector)
        self._last_frame_at = 0
        database.update_camera_status(self.id, self.name, "starting")

    def start(self):
        pass  # nothing to start - frames arrive via ingest_frame()

    def stop(self):
        self.status = "offline"
        database.update_camera_status(self.id, self.name, "offline")

    def is_stale(self):
        return (
            self._last_frame_at
            and time.time() - self._last_frame_at > self.STALE_AFTER_SECONDS
        )

    def ingest_frame(self, jpeg_bytes):
        """Called from a Flask request handler when a browser uploads a frame."""
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return False

        frame = cv2.resize(frame, (config.FRAME_WIDTH, config.FRAME_HEIGHT))
        self._frame_count += 1
        self._last_frame_at = time.time()

        display_frame = frame
        if self._frame_count % config.PROCESS_EVERY_N_FRAMES == 0:
            display_frame = self._process_frame(frame)

        with self._frame_lock:
            self._latest_frame = display_frame

        if self.status != "online":
            self.status = "online"
        database.update_camera_status(self.id, self.name, "online")
        return True
