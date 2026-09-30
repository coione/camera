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

**Focus control** — `_configure_camera()` runs on every camera open (initial and reconnect). It disables the sensor's continuous autofocus, whose refocus events are indistinguishable from whole-frame motion to a frame-difference detector. The driver reports `focus_absolute` as `inactive` while autofocus is on, so the position autofocus converges to cannot be read back and frozen; instead `_autofocus_sweep()` does a coarse-to-fine sweep of the focus range and keeps the position with the highest Laplacian variance. The result is cached in `_locked_focus` (guarded by `_focus_resolved`) so a mid-run reconnect reapplies it without another ~10 s sweep. `CAMERA_FOCUS` skips the sweep entirely; `CAMERA_AUTOFOCUS=true` restores the old behaviour.

The `/photo` command handler reads `_latest_frame` directly — it never touches `VideoCapture` itself, avoiding any camera contention. The `/video` command cannot read `VideoCapture` directly (the motion thread owns it), so it enqueues the requesting chat ID onto `_video_requests`; the motion thread drains that queue, records the clip, and sends it. Motion-triggered clips and `/video` share the same `_record_clip()` helper.

## Key Details

- `opencv-python-headless` is used (not `opencv-python`) to avoid GUI toolkit dependencies on the headless Pi.
- `_record_clip()` pipes raw frames to `ffmpeg` for H.264/MP4 encoding with `+faststart` — OpenCV's own `VideoWriter` only produces mp4v, which most Telegram clients fail to play inline. `ffmpeg` must be installed on the host (`sudo apt install ffmpeg`).
- After writing frames, `_record_clip()` reads `proc.stderr` and calls `proc.wait()` directly instead of `proc.communicate()`: on Python 3.13 `communicate()` unconditionally flushes stdin, raising `ValueError: flush of closed file` since stdin was already closed.
- Both the motion-clip and `/video` recording calls are wrapped in `try/except` so a recording failure logs an error and continues rather than killing the motion-detection thread.
- If a frame read fails, the motion loop releases and reopens `VideoCapture` rather than exiting.
- The motion thread must be stopped explicitly, via the `_shutdown` event set by the `post_shutdown` hook, rather than left to die as a daemon. CPython stops a daemon thread by unwinding out of whatever call it is in; here that is almost always an OpenCV (C++) call, and unwinding through a C++ frame with no handler reaches `std::terminate` — the process then dies with SIGABRT and `FATAL: exception not rethrown` instead of exit code 0. `_record_clip()` and `_autofocus_sweep()` check the same flag so an in-progress recording or sweep aborts in well under a second; the thread stays `daemon=True` only so a timed-out join cannot hang the process.
- `_require_env()` calls `sys.exit` with a clear message if `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` are missing.
- Motion is only reported after three filters pass, all tunable via `.env`: `MOTION_MAX_FOCUS_SHIFT` (frame-to-frame ratio of Laplacian variance — a refocus or lighting change alters global sharpness, a moving object does not, measured at `6.2x` vs `1.19x` on the reference camera), `MOTION_MAX_CHANGE_RATIO` (fraction of changed pixels, catches exposure/white-balance steps), and `MOTION_CONSEC_FRAMES` (consecutive positive readings required). The focus measure must be computed on the **unblurred** grayscale frame — the detector's 21x21 Gaussian erases the high-frequency detail it reads. `_reference()` returns the `(blurred_gray, focus_measure)` pair and is used at every point the loop refreshes its comparison baseline; a stale `prev_sharpness` would otherwise fake a sharpness spike on resume.
- `MOTION_MIN_AREA`, `MOTION_COOLDOWN`, `VIDEO_DURATION`, and `CAMERA_INDEX` are all runtime-tunable via `.env` — no code changes needed.
- The systemd unit sets `Group=video` so the process can access `/dev/video*`.
