# 🐍 Snake Watch — AI CCTV Snake Detection System

24/7 snake monitoring using RTSP CCTV cameras + your trained YOLO `best.pt`
model, with a live web dashboard, screenshots, alarm, and SQLite history.

## Project layout

```
snake_detection_system/
├── app.py              # Flask app entry point — run this
├── config.py           # ALL settings: cameras, model path, thresholds
├── database.py         # SQLite schema + queries
├── detector.py          # YOLO model wrapper (ultralytics)
├── camera_stream.py     # Per-camera thread: RTSP + reconnect + detection
├── alarm.py             # Sound alarm on confirmed detection
├── templates/
│   └── dashboard.html   # Web dashboard UI
├── static/
│   ├── screenshots/      # Auto-saved detection evidence images
│   └── alarm/alarm.wav   # <- put an alarm sound file here
├── models/
│   └── best.pt           # <- put your trained YOLO weights here
├── detections.db          # created automatically on first run
└── requirements.txt
```

## Setup

```bash
cd snake_detection_system
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

1. Copy your trained model into `models/best.pt`.
2. Copy or download any `.wav` alarm sound into `static/alarm/alarm.wav`.
3. Open `config.py` and edit the `CAMERAS` list with your real RTSP URLs
   and location names (Main Gate, Farm Area, Storage Room, etc.).

## Run

```bash
python app.py
```

Then open **http://localhost:5000** (or `http://<machine-ip>:5000` from
another device on the same network).

## Recommended build/test order (matches your plan)

1. **CCTV connection** — In `config.py`, set ONE camera. For a quick test
   without real CCTV, set `rtsp_url` to `"0"` to use your laptop webcam.
   Run `app.py` and confirm the live feed shows on the dashboard.
2. **YOLO detection** — Confirm `models/best.pt` loads (check the console
   log on startup) and that boxes appear on the feed when a snake is
   in frame.
3. **Screenshot** — Confirm images appear in `static/screenshots/` and
   in the "Detection History" table on a real detection.
4. **Alarm** — Confirm `static/alarm/alarm.wav` plays on detection.
5. **Database** — Inspect `detections.db` with any SQLite viewer, or just
   trust the dashboard table (reads straight from it).
6. **Dashboard** — Already wired up; refreshes automatically every
   `DASHBOARD_REFRESH_SECONDS`.
7. **Multiple cameras** — Once step 1–6 are stable on one camera, add more
   entries to `CAMERAS` in `config.py` one at a time, watching RAM usage
   (`htop` / Task Manager) after each addition.
8. **24/7 monitoring** — Already handled: each camera reconnects
   automatically (`RECONNECT_DELAY_SECONDS`) if the RTSP stream drops.
   For real deployment, also run this under a process supervisor (see
   below) so it restarts if the whole script crashes.

## Authentication (do this before deploying anywhere beyond your own laptop)

The dashboard and every API route are locked behind a login screen by
default (`config.AUTH_ENABLED = True`). Before running this anywhere
reachable by other people, change the default credentials:

1. Pick a real password and generate its hash:
   ```bash
   python -c "from werkzeug.security import generate_password_hash as g; print(g('your-new-password'))"
   ```
2. Paste the output into `config.py` as `AUTH_PASSWORD_HASH`.
3. Change `AUTH_USERNAME` if you don't want to keep "admin".
4. Change `SECRET_KEY` in `config.py` to any long random string, e.g.:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   (this signs the login session cookie — anyone who knows it could forge a
   logged-in session, so it must be unique to your deployment)

The default username/password is `admin` / `changeme` — this is only safe
on your own machine during initial setup. If you ever want to disable the
login screen entirely (e.g. purely local testing), set
`AUTH_ENABLED = False` in `config.py`.

## Tuning for a 4 GB RAM laptop

All in `config.py`:
- `FRAME_WIDTH` / `FRAME_HEIGHT` — keep at 640×480 or lower.
- `PROCESS_EVERY_N_FRAMES` — raise this (e.g. 8–10) if CPU/RAM is tight;
  it skips YOLO on most frames and only inspects every Nth frame.
- `DEVICE = "cpu"` — leave as CPU unless you have a dedicated GPU.
- Start with **one** camera, confirm stable memory usage over 30+ minutes,
  then add more one at a time.

## Keeping it running 24/7 (process supervision)

The app already reconnects dropped *camera streams* automatically. To also
auto-restart the whole Python process if it ever crashes:

**Linux (systemd)** — create `/etc/systemd/system/snakewatch.service`:
```ini
[Unit]
Description=Snake Watch CCTV Monitoring
After=network.target

[Service]
WorkingDirectory=/path/to/snake_detection_system
ExecStart=/path/to/snake_detection_system/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Then: `sudo systemctl enable --now snakewatch`

**Windows** — use Task Scheduler with "Restart on failure", or run inside
NSSM (Non-Sucking Service Manager) as a Windows service.

## Notes

- `SNAKE_CLASS_NAMES` in `config.py` must match the exact class name(s)
  your `best.pt` was trained with — check by printing `model.names` or
  looking at your training `data.yaml`.
- `DETECTION_COOLDOWN_SECONDS` prevents one snake sitting in frame from
  spamming the database/alarm every single frame — it logs once, then
  waits before logging again from the same camera.
- The video feed on the dashboard is MJPEG over HTTP, viewable directly
  in any browser tab at `/video_feed/<camera_id>` too.
