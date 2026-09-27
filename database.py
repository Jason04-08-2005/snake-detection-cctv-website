"""
database.py
SQLite storage for detection history + live camera status.
Uses a lock because multiple camera threads write concurrently.
"""

import sqlite3
import threading
from datetime import datetime

import config

_lock = threading.Lock()


def get_connection():
    conn = sqlite3.connect(config.DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL,
                camera_name TEXT NOT NULL,
                confidence REAL NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                image_path TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS camera_status (
                camera_id TEXT PRIMARY KEY,
                camera_name TEXT NOT NULL,
                status TEXT NOT NULL,
                last_frame_at TEXT,
                last_detection_at TEXT,
                reconnect_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()


def log_detection(camera_id, camera_name, confidence, image_path):
    now = datetime.now()
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO detections
                (camera_id, camera_name, confidence, date, time, image_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            camera_id,
            camera_name,
            confidence,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            image_path,
            now.isoformat(),
        ))
        conn.commit()
        conn.close()


def update_camera_status(camera_id, camera_name, status, detection=False):
    now = datetime.now().isoformat()
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO camera_status (camera_id, camera_name, status, last_frame_at, last_detection_at, reconnect_count)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(camera_id) DO UPDATE SET
                camera_name = excluded.camera_name,
                status = excluded.status,
                last_frame_at = ?,
                last_detection_at = CASE WHEN ? = 1 THEN ? ELSE last_detection_at END
        """, (camera_id, camera_name, status, now, now if detection else None,
              now, 1 if detection else 0, now))
        conn.commit()
        conn.close()


def increment_reconnect(camera_id):
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE camera_status SET reconnect_count = reconnect_count + 1
            WHERE camera_id = ?
        """, (camera_id,))
        conn.commit()
        conn.close()


def get_recent_detections(limit=50):
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM detections ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows


def get_all_camera_status():
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM camera_status")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows


def get_stats():
    with _lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM detections")
        total = cur.fetchone()["total"]
        cur.execute("""
            SELECT COUNT(*) as today FROM detections
            WHERE date = ?
        """, (datetime.now().strftime("%Y-%m-%d"),))
        today = cur.fetchone()["today"]
        conn.close()
        return {"total_detections": total, "today_detections": today}
