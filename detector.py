"""
detector.py
Thin wrapper around the YOLO model (best.pt) via ultralytics.
Loaded ONCE and shared across all camera threads (thread-safe enough for
sequential .predict() calls; ultralytics internally serializes via GIL-bound
numpy/torch ops, but we still guard with a lock to be safe on low-RAM boxes
where you don't want overlapping inference calls competing for memory).
"""

import threading

import config


class SnakeDetector:
    def __init__(self):
        self._lock = threading.Lock()
        self.model = None
        self._load_model()

    def _load_model(self):
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise RuntimeError(
                "ultralytics is not installed. Run: pip install ultralytics"
            ) from e

        import os
        if not os.path.exists(config.MODEL_PATH):
            raise FileNotFoundError(
                f"Model weights not found at {config.MODEL_PATH}. "
                f"Copy your trained best.pt into the models/ folder."
            )

        print(f"[Detector] Loading model from {config.MODEL_PATH} ...")
        self.model = YOLO(config.MODEL_PATH)
        self.model.to(config.DEVICE)
        print("[Detector] Model loaded.")

    def detect(self, frame):
        """
        Runs inference on a single BGR frame (numpy array).
        Returns a list of detections: [{"class_name": str, "confidence": float, "box": (x1,y1,x2,y2)}, ...]
        Only returns detections matching SNAKE_CLASS_NAMES and above CONFIDENCE_THRESHOLD.
        """
        with self._lock:
            results = self.model.predict(
                source=frame,
                conf=config.CONFIDENCE_THRESHOLD,
                device=config.DEVICE,
                verbose=False,
            )

        detections = []
        if not results:
            return detections

        result = results[0]
        names = result.names  # class_id -> class_name

        for box in result.boxes:
            cls_id = int(box.cls[0])
            class_name = names.get(cls_id, str(cls_id))
            confidence = float(box.conf[0])

            if class_name.lower() not in [n.lower() for n in config.SNAKE_CLASS_NAMES]:
                continue
            if confidence < config.CONFIDENCE_THRESHOLD:
                continue

            x1, y1, x2, y2 = map(int, box.xyxy[0])
            detections.append({
                "class_name": class_name,
                "confidence": confidence,
                "box": (x1, y1, x2, y2),
            })

        return detections

    @staticmethod
    def draw_boxes(frame, detections):
        import cv2
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            label = f'{det["class_name"]} {det["confidence"]*100:.1f}%'
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(frame, label, (x1, max(y1 - 10, 15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        return frame
