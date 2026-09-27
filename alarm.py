"""
alarm.py
Plays an alarm sound when a snake is confirmed. Runs playback in its own
thread so it never blocks camera capture/inference. Has a global cooldown
so multiple cameras detecting at once don't overlap alarms endlessly.
"""

import os
import threading
import time

import config

_last_alarm_time = 0
_alarm_lock = threading.Lock()


def _play_sound():
    """Try a few common playback backends; degrade gracefully if none exist."""
    try:
        # playsound is lightweight and cross-platform
        from playsound import playsound
        playsound(config.ALARM_SOUND_PATH)
        return
    except Exception:
        pass

    try:
        # Linux fallback
        if os.name == "posix":
            os.system(f'aplay "{config.ALARM_SOUND_PATH}" >/dev/null 2>&1')
            return
    except Exception:
        pass

    try:
        # Windows fallback
        import winsound
        winsound.PlaySound(config.ALARM_SOUND_PATH, winsound.SND_FILENAME)
        return
    except Exception:
        pass

    print("[ALARM] 🚨 SNAKE DETECTED! (no audio backend available to play sound)")


def trigger_alarm(camera_name, confidence):
    """Non-blocking: fires a background thread to play the alarm, respecting cooldown."""
    global _last_alarm_time

    if not config.ALARM_ENABLED:
        return

    with _alarm_lock:
        now = time.time()
        if now - _last_alarm_time < config.ALARM_COOLDOWN_SECONDS:
            return
        _last_alarm_time = now

    print(f"[ALARM] 🚨 Snake detected at '{camera_name}' ({confidence*100:.1f}% confidence)")

    if os.path.exists(config.ALARM_SOUND_PATH):
        threading.Thread(target=_play_sound, daemon=True).start()
    else:
        print(f"[ALARM] Sound file not found at {config.ALARM_SOUND_PATH} - add a .wav there.")
