"""
app.py
Entry point. Loads the YOLO model once at startup, then lets the dashboard
add, start, and stop cameras dynamically (laptop webcam / phone camera /
custom RTSP-IP URL) via a small JSON API, rather than requiring every
camera to be predefined in config.py.

The dashboard, live video feeds, and every /api/* route are protected by a
login screen (see config.AUTH_ENABLED / AUTH_USERNAME / AUTH_PASSWORD_HASH).

Run:
    python app.py
Then open:
    http://<this-machine-ip>:5000
"""

import time
import uuid
import threading
from datetime import timedelta
from functools import wraps

from flask import (
    Flask, Response, render_template, jsonify, request,
    session, redirect, url_for,
)
from werkzeug.security import check_password_hash

import config
import database
from detector import SnakeDetector
from camera_stream import CameraWorker, BrowserCameraWorker

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=config.SESSION_LIFETIME_HOURS)

workers = {}          # camera_id -> CameraWorker
workers_lock = threading.Lock()
detector = None        # set once in start_system()


def start_system():
    global detector
    database.init_db()

    print("[System] Loading YOLO model (this can take a moment)...")
    detector = SnakeDetector()
    print("[System] Model loaded. Ready to accept cameras.")

    # Optional: auto-start any cameras predefined in config.py
    active_cams = [c for c in config.CAMERAS if c.get("enabled", True)]
    for cam_cfg in active_cams:
        _start_worker(cam_cfg["id"], cam_cfg["name"], cam_cfg["rtsp_url"])
        time.sleep(1)  # stagger startup

    threading.Thread(target=_browser_camera_watchdog, daemon=True).start()


def _browser_camera_watchdog():
    """
    Browser cameras have no capture thread of their own to notice a dropped
    connection (e.g. the phone's browser tab was closed without pressing
    Stop). This periodically checks for stale ones and marks them offline.
    """
    while True:
        time.sleep(3)
        with workers_lock:
            snapshot = list(workers.items())
        for camera_id, worker in snapshot:
            if isinstance(worker, BrowserCameraWorker) and worker.is_stale():
                worker.status = "offline"
                database.update_camera_status(camera_id, worker.name, "offline")


def _build_source(source_type, source_value):
    """
    Translates a dashboard source selection into the value CameraWorker
    understands (an RTSP/HTTP URL string, or a webcam index as a string).
    Not used for source_type == "browser", which has no URL/index at all.
    """
    source_type = (source_type or "").strip().lower()
    source_value = (source_value or "").strip()

    if source_type == "webcam":
        # source_value is a device index e.g. "0", "1"; default to 0
        return source_value if source_value != "" else "0"

    if source_type == "custom":
        if not source_value:
            raise ValueError("A camera URL is required for this source type.")
        return source_value

    raise ValueError(f"Unknown source_type '{source_type}'.")


def _start_worker(camera_id, name, source):
    worker = CameraWorker({"id": camera_id, "name": name, "rtsp_url": source}, detector)
    with workers_lock:
        workers[camera_id] = worker
    worker.start()
    print(f"[System] Started worker '{name}' ({camera_id}) -> {source}")
    return worker


def _start_browser_worker(camera_id, name):
    worker = BrowserCameraWorker(camera_id, name, detector)
    with workers_lock:
        workers[camera_id] = worker
    print(f"[System] Registered browser-camera worker '{name}' ({camera_id}); waiting for frames.")
    return worker


# ---------------------------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not config.AUTH_ENABLED:
            return view(*args, **kwargs)
        if session.get("logged_in"):
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized. Please log in."}), 401
        return redirect(url_for("login_page", next=request.path))
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not config.AUTH_ENABLED:
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        if session.get("logged_in"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", error=None)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    valid = (
        username == config.AUTH_USERNAME
        and check_password_hash(config.AUTH_PASSWORD_HASH, password)
    )

    if not valid:
        return render_template("login.html", error="Incorrect username or password."), 401

    session.permanent = True
    session["logged_in"] = True
    session["username"] = username
    next_path = request.args.get("next") or url_for("dashboard")
    return redirect(next_path)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        refresh_seconds=config.DASHBOARD_REFRESH_SECONDS,
        auth_enabled=config.AUTH_ENABLED,
        username=session.get("username"),
    )


@app.route("/video_feed/<camera_id>")
@login_required
def video_feed(camera_id):
    worker = workers.get(camera_id)
    if worker is None:
        return "Camera not found", 404

    def generate():
        while True:
            if camera_id not in workers:
                break  # camera was stopped - end the stream
            jpeg = worker.get_latest_jpeg()
            if jpeg is not None:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
            time.sleep(0.05)  # ~20 fps cap on stream sent to browser

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/cameras", methods=["GET"])
@login_required
def api_cameras_list():
    """Returns every camera currently known (running or recently running)."""
    statuses = {row["camera_id"]: row for row in database.get_all_camera_status()}
    out = []
    with workers_lock:
        camera_ids = list(workers.keys())
    for cid in camera_ids:
        row = statuses.get(cid, {})
        out.append({
            "id": cid,
            "name": row.get("camera_name", cid),
            "status": row.get("status", "starting"),
            "last_frame_at": row.get("last_frame_at"),
            "last_detection_at": row.get("last_detection_at"),
            "reconnect_count": row.get("reconnect_count", 0),
        })
    return jsonify({"cameras": out, "stats": database.get_stats()})


@app.route("/api/cameras", methods=["POST"])
@login_required
def api_cameras_create():
    """
    Body JSON: { "name": str, "source_type": "webcam"|"browser"|"custom", "source_value": str }
    "browser" needs no source_value - the browser pushes frames itself after
    this call, via POST /api/cameras/<id>/frame.
    Starts a new camera worker and returns its assigned id.
    """
    if detector is None:
        return jsonify({"error": "Model still loading, try again shortly."}), 503

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "Camera"
    source_type = (data.get("source_type") or "").strip().lower()
    source_value = data.get("source_value", "")

    camera_id = "cam_" + uuid.uuid4().hex[:8]

    if source_type == "browser":
        _start_browser_worker(camera_id, name)
        return jsonify({"id": camera_id, "name": name, "status": "starting", "mode": "browser"}), 201

    try:
        source = _build_source(source_type, source_value)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    _start_worker(camera_id, name, source)
    return jsonify({"id": camera_id, "name": name, "status": "starting", "mode": "pull"}), 201


@app.route("/api/cameras/<camera_id>/frame", methods=["POST"])
@login_required
def api_cameras_frame(camera_id):
    """
    Receives one JPEG frame from a browser that granted camera permission.
    Expects the raw JPEG bytes as the request body
    (Content-Type: application/octet-stream or image/jpeg).
    """
    worker = workers.get(camera_id)
    if worker is None:
        return jsonify({"error": "Camera not found"}), 404
    if not isinstance(worker, BrowserCameraWorker):
        return jsonify({"error": "This camera does not accept pushed frames"}), 400

    jpeg_bytes = request.get_data()
    if not jpeg_bytes:
        return jsonify({"error": "No frame data received"}), 400

    ok = worker.ingest_frame(jpeg_bytes)
    if not ok:
        return jsonify({"error": "Could not decode frame"}), 400

    return jsonify({"ok": True})


@app.route("/api/cameras/<camera_id>/stop", methods=["POST"])
@login_required
def api_cameras_stop(camera_id):
    with workers_lock:
        worker = workers.pop(camera_id, None)
    if worker is None:
        return jsonify({"error": "Camera not found"}), 404

    worker.stop()
    database.update_camera_status(camera_id, worker.name, "offline")
    return jsonify({"id": camera_id, "status": "stopped"})


@app.route("/api/status")
@login_required
def api_status():
    # Kept for backward compatibility; same data as GET /api/cameras
    return api_cameras_list()


@app.route("/api/detections")
@login_required
def api_detections():
    limit = 50
    return jsonify(database.get_recent_detections(limit=limit))


if __name__ == "__main__":
    start_system()
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, threaded=True)
