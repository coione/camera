# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Set up
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Run
venv/bin/python camera_bot.py

# Systemd service management
sudo systemctl start|stop|restart|status camera-bot
journalctl -u camera-bot -f
```

## Architecture

Single-file application (`camera_bot.py`) with two concurrent execution paths sharing one process:

**Motion-detection thread** (`threading.Thread`, daemon) — the sole owner of the `cv2.VideoCapture` object. Runs at ~10 fps, stores each frame in the module-level `_latest_frame` (guarded by `_frame_lock`), and performs frame-difference analysis (Gaussian blur → `absdiff` → binary threshold → dilate → contour area check). On motion, it records a `VIDEO_DURATION`-second clip via `_record_clip()` (H.264/MP4 through ffmpeg) and schedules a `bot.send_video()` coroutine onto the asyncio event loop using `asyncio.run_coroutine_threadsafe()`. Because recording consumes the camera for the clip's duration, the thread refreshes its grayscale reference frame afterwards to avoid a stale-diff false positive on the next iteration.

**Telegram bot (asyncio)** — driven by `python-telegram-bot` v21 `Application.run_polling()`. The `_post_init` hook (registered via `Application.builder().post_init(...)`) is where the motion thread is launched; this is the earliest point where `asyncio.get_running_loop()` is available, and the thread needs that loop reference to schedule coroutines from outside asyncio.

The `/photo` command handler reads `_latest_frame` directly — it never touches `VideoCapture` itself, avoiding any camera contention. The `/video` command cannot read `VideoCapture` directly (the motion thread owns it), so it enqueues the requesting chat ID onto `_video_requests`; the motion thread drains that queue, records the clip, and sends it. Motion-triggered clips and `/video` share the same `_record_clip()` helper.

## Key Details

- `opencv-python-headless` is used (not `opencv-python`) to avoid GUI toolkit dependencies on the headless Pi.
- `_record_clip()` pipes raw frames to `ffmpeg` for H.264/MP4 encoding with `+faststart` — OpenCV's own `VideoWriter` only produces mp4v, which most Telegram clients fail to play inline. `ffmpeg` must be installed on the host (`sudo apt install ffmpeg`).
- After writing frames, `_record_clip()` reads `proc.stderr` and calls `proc.wait()` directly instead of `proc.communicate()`: on Python 3.13 `communicate()` unconditionally flushes stdin, raising `ValueError: flush of closed file` since stdin was already closed.
- Both the motion-clip and `/video` recording calls are wrapped in `try/except` so a recording failure logs an error and continues rather than killing the motion-detection thread.
- If a frame read fails, the motion loop releases and reopens `VideoCapture` rather than exiting.
- `_require_env()` calls `sys.exit` with a clear message if `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` are missing.
- `MOTION_MIN_AREA`, `MOTION_COOLDOWN`, `VIDEO_DURATION`, and `CAMERA_INDEX` are all runtime-tunable via `.env` — no code changes needed.
- The systemd unit sets `Group=video` so the process can access `/dev/video*`.
