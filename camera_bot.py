#!/usr/bin/env python3
"""
Raspberry Pi Camera Telegram Bot

Continuously monitors a USB camera for motion and sends a short Telegram video
clip on each event. Also responds to /photo and /video for on-demand captures.
Supports /motion_on, /motion_off, /motion_status to control detection at runtime.
"""

import asyncio
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from io import BytesIO

import cv2
from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"ERROR: Required environment variable '{name}' is not set.")
    return value


BOT_TOKEN       = _require_env("TELEGRAM_BOT_TOKEN")
CHAT_ID         = _require_env("TELEGRAM_CHAT_ID")
CAMERA_INDEX    = int(os.getenv("CAMERA_INDEX", "0"))
MOTION_MIN_AREA = int(os.getenv("MOTION_MIN_AREA", "3000"))  # pixels²
MOTION_COOLDOWN = int(os.getenv("MOTION_COOLDOWN", "10"))    # seconds between alerts
VIDEO_DURATION  = int(os.getenv("VIDEO_DURATION", "5"))      # seconds captured by /video

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("camera_bot")

# ---------------------------------------------------------------------------
# Shared camera state — written by the motion-detection thread,
# read by the /photo command handler.
# ---------------------------------------------------------------------------

_latest_frame = None
_frame_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Motion-detection enabled flag — set/cleared by /motion_on and /motion_off.
# ---------------------------------------------------------------------------

_motion_enabled = threading.Event()
_motion_enabled.set()  # on by default

# ---------------------------------------------------------------------------
# /video requests — enqueued by the command handler (asyncio side), drained
# by the motion-detection thread since it is the sole owner of VideoCapture.
# Each item is the chat_id to send the resulting clip to.
# ---------------------------------------------------------------------------

_video_requests: "queue.Queue[int]" = queue.Queue()


def _set_frame(frame) -> None:
    global _latest_frame
    with _frame_lock:
        _latest_frame = frame


def _get_frame():
    with _frame_lock:
        return _latest_frame


def _encode_jpeg(frame) -> BytesIO:
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    bio = BytesIO(buf.tobytes())
    bio.name = "capture.jpg"
    return bio


def _record_clip(cap: "cv2.VideoCapture", duration: float) -> str | None:
    """
    Captures frames from `cap` for `duration` seconds and encodes them to a
    temporary H.264 MP4 (via ffmpeg, with +faststart) so Telegram clients can
    play the clip inline instead of showing an endless loading spinner —
    OpenCV's own VideoWriter only offers MPEG-4 (mp4v), which most Telegram
    clients fail to decode. Must be called from the motion-detection thread,
    the sole owner of `cap`. Returns the file path, or None on failure.
    """
    frames = []
    start = time.monotonic()
    while time.monotonic() - start < duration:
        ret, frame = cap.read()
        if not ret:
            break
        frames.append(frame)
        _set_frame(frame)

    if not frames:
        return None

    elapsed = time.monotonic() - start
    fps = len(frames) / elapsed if elapsed > 0 else 10.0
    height, width = frames[0].shape[:2]

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", f"{fps:.3f}",
            "-i", "-",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            tmp.name,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for frame in frames:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    # Read stderr and wait directly instead of proc.communicate(): on
    # Python 3.13 communicate() unconditionally flushes stdin, which raises
    # "ValueError: flush of closed file" once we've already closed it above.
    stderr = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0:
        logger.error("ffmpeg encoding failed: %s", stderr.decode(errors="replace")[-500:])
        os.remove(tmp.name)
        return None

    return tmp.name


# ---------------------------------------------------------------------------
# Motion-detection thread
# ---------------------------------------------------------------------------

def motion_detection_loop(bot, loop: asyncio.AbstractEventLoop) -> None:
    """
    Runs in a daemon thread.

    Opens the USB camera once, continuously reads frames at ~10 fps, and
    detects motion using frame-difference analysis (Gaussian blur → absdiff →
    threshold → dilate → contours). When a significant contour is found and
    the cooldown has elapsed, schedules a Telegram send_photo on the asyncio
    event loop via run_coroutine_threadsafe().
    """
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        logger.error("Cannot open camera at index %d", CAMERA_INDEX)
        return

    logger.info("Camera opened (index=%d)", CAMERA_INDEX)
    time.sleep(1.0)  # allow the sensor to auto-adjust exposure/white-balance

    ret, frame = cap.read()
    if not ret:
        logger.error("Failed to read first frame — aborting motion loop")
        cap.release()
        return

    _set_frame(frame)
    prev_gray = cv2.GaussianBlur(
        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0
    )
    last_alert: float = 0.0

    logger.info(
        "Motion detection active (min_area=%d px², cooldown=%d s)",
        MOTION_MIN_AREA, MOTION_COOLDOWN,
    )

    while True:
        try:
            chat_id = _video_requests.get_nowait()
        except queue.Empty:
            chat_id = None

        if chat_id is not None:
            logger.info("Recording %ds video clip for chat %s", VIDEO_DURATION, chat_id)
            try:
                path = _record_clip(cap, VIDEO_DURATION)
            except Exception as exc:
                logger.error("Video recording failed: %s", exc)
                path = None
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            async def _send_clip(p=path, c=chat_id, t=ts):
                if p is None:
                    await bot.send_message(chat_id=c, text="Failed to capture video — camera read error.")
                    return
                try:
                    with open(p, "rb") as f:
                        await bot.send_video(chat_id=c, video=f, caption=f"Captured at {t}")
                    logger.info("Video sent to chat %s", c)
                except Exception as exc:
                    logger.error("Failed to send video: %s", exc)
                finally:
                    os.remove(p)

            asyncio.run_coroutine_threadsafe(_send_clip(), loop)

            # Recording paused frame reads for VIDEO_DURATION seconds — the
            # previous grayscale reference is stale, so refresh it.
            ret, frame = cap.read()
            if ret:
                _set_frame(frame)
                prev_gray = cv2.GaussianBlur(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0
                )
            continue

        if not _motion_enabled.is_set():
            # Detection is paused — keep reading frames so _latest_frame stays
            # fresh (for /photo) and to avoid stale prev_gray on resume.
            ret, frame = cap.read()
            if ret:
                _set_frame(frame)
                prev_gray = cv2.GaussianBlur(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0
                )
            time.sleep(0.1)
            continue

        ret, frame = cap.read()
        if not ret:
            logger.warning("Frame read failed — reopening camera in 2 s")
            cap.release()
            time.sleep(2.0)
            cap = cv2.VideoCapture(CAMERA_INDEX)
            continue

        _set_frame(frame)

        # Frame-difference motion detection
        gray   = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        delta  = cv2.absdiff(prev_gray, gray)
        thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        prev_gray = gray

        now = time.monotonic()
        if (
            any(cv2.contourArea(c) > MOTION_MIN_AREA for c in contours)
            and now - last_alert >= MOTION_COOLDOWN
        ):
            last_alert = now
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info("Motion detected — recording %ds clip", VIDEO_DURATION)
            try:
                path = _record_clip(cap, VIDEO_DURATION)
            except Exception as exc:
                logger.error("Motion clip recording failed: %s", exc)
                path = None

            async def _alert(p=path, t=ts):
                if p is None:
                    logger.error("Motion clip capture failed — no video sent")
                    return
                try:
                    with open(p, "rb") as f:
                        await bot.send_video(
                            chat_id=CHAT_ID,
                            video=f,
                            caption=f"Motion detected at {t}",
                        )
                    logger.info("Motion alert sent (%s)", t)
                except Exception as exc:
                    logger.error("Failed to send motion alert: %s", exc)
                finally:
                    os.remove(p)

            asyncio.run_coroutine_threadsafe(_alert(), loop)

            # Recording consumed VIDEO_DURATION seconds of frames, so the
            # grayscale reference is stale — refresh it before the next diff.
            ret, frame = cap.read()
            if ret:
                _set_frame(frame)
                prev_gray = cv2.GaussianBlur(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0
                )

        time.sleep(0.1)  # ~10 fps polling rate


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Camera bot is running.\n\n"
        "/photo — capture a photo on demand\n"
        f"/video — capture a {VIDEO_DURATION}s video on demand\n"
        "/motion_on — enable motion detection\n"
        "/motion_off — disable motion detection\n"
        "/motion_status — show current motion detection state"
    )


async def cmd_motion_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _motion_enabled.set()
    logger.info("Motion detection enabled by user %s", update.effective_user.id)
    await update.message.reply_text("Motion detection has been activated.")


async def cmd_motion_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _motion_enabled.clear()
    logger.info("Motion detection disabled by user %s", update.effective_user.id)
    await update.message.reply_text("Motion detection has been deactivated.")


async def cmd_motion_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = "ON" if _motion_enabled.is_set() else "OFF"
    await update.message.reply_text(f"Motion detection is currently {state}.")


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    frame = _get_frame()
    if frame is None:
        await update.message.reply_text(
            "Camera not ready yet — please try again in a moment."
        )
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_photo(
        photo=_encode_jpeg(frame),
        caption=f"Captured at {ts}",
    )
    logger.info("/photo served to user %s at %s", update.effective_user.id, ts)


async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _video_requests.put(chat_id)
    logger.info("/video requested by user %s", update.effective_user.id)
    await update.message.reply_text(f"Recording a {VIDEO_DURATION}s video…")


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------

async def _post_init(application: Application) -> None:
    """
    Called by python-telegram-bot after the app initialises but before
    polling begins. The running event loop is already available here, so
    this is the correct place to start the motion-detection thread.
    """
    loop = asyncio.get_running_loop()
    thread = threading.Thread(
        target=motion_detection_loop,
        args=(application.bot, loop),
        name="motion-detection",
        daemon=True,
    )
    thread.start()
    logger.info("Motion-detection thread started")

    await application.bot.set_my_commands([
        BotCommand("photo",          "Capture a photo on demand"),
        BotCommand("video",          "Capture a short video on demand"),
        BotCommand("motion_on",      "Enable motion detection"),
        BotCommand("motion_off",     "Disable motion detection"),
        BotCommand("motion_status",  "Show motion detection state"),
    ])


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("photo",          cmd_photo))
    app.add_handler(CommandHandler("video",          cmd_video))
    app.add_handler(CommandHandler("motion_on",      cmd_motion_on))
    app.add_handler(CommandHandler("motion_off",     cmd_motion_off))
    app.add_handler(CommandHandler("motion_status",  cmd_motion_status))

    logger.info("Starting camera bot")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
