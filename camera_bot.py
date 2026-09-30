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
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"ERROR: Required environment variable '{name}' is not set.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN       = _require_env("TELEGRAM_BOT_TOKEN")
CHAT_ID         = _require_env("TELEGRAM_CHAT_ID")
CAMERA_INDEX    = int(os.getenv("CAMERA_INDEX", "0"))


def _allowed_users() -> set[int]:
    """
    Telegram user IDs permitted to issue commands.

    A bot is reachable by anyone who learns its username, so without this
    every stranger could run /photo and /video and watch the room, or
    /motion_off and silence the alarm. Defaults to the alert recipient: for a
    private chat Telegram uses the same ID for the chat and the user, so the
    owner stays in without configuring anything. A negative TELEGRAM_CHAT_ID
    is a group, whose ID is not a user ID, so that case must be spelled out.
    """
    raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    ids = {int(part) for part in raw.replace(",", " ").split() if part.strip("-").isdigit()}
    if ids:
        return ids
    if CHAT_ID.isdigit():
        return {int(CHAT_ID)}
    sys.exit(
        "ERROR: TELEGRAM_CHAT_ID is a group, so it cannot double as the "
        "command allowlist. Set TELEGRAM_ALLOWED_USERS to the numeric user "
        "IDs allowed to control the bot."
    )


ALLOWED_USERS = _allowed_users()
MOTION_MIN_AREA = int(os.getenv("MOTION_MIN_AREA", "3000"))  # pixels²
MOTION_COOLDOWN = int(os.getenv("MOTION_COOLDOWN", "10"))    # seconds between alerts
VIDEO_DURATION  = int(os.getenv("VIDEO_DURATION", "5"))      # seconds captured by /video

# Continuous autofocus is the main source of false motion alerts: each refocus
# takes the entire frame from blurry to sharp at once, which frame-difference
# analysis cannot tell apart from a large moving object. A fixed camera never
# needs to refocus, so the lens is pinned at startup instead.
CAMERA_AUTOFOCUS = _env_bool("CAMERA_AUTOFOCUS", False)      # true restores continuous AF
_focus_env       = os.getenv("CAMERA_FOCUS")
CAMERA_FOCUS     = int(_focus_env) if _focus_env else None   # unset → sweep for it at startup

# Backstops for the global changes that survive a locked lens — an exposure or
# white-balance step, a light switched on, a refocus if autofocus is re-enabled.
#   MAX_CHANGE_RATIO — reject when this fraction of the frame changes at once.
#   MAX_FOCUS_SHIFT  — reject when image sharpness jumps by this factor between
#     readings. Measured on this camera: a static scene holds 1.01x and a large
#     moving object 1.19x, while a refocus spikes to 6.2x — so the sharpness
#     jump, not the changed area, is what actually separates focus from motion.
#   CONSEC_FRAMES    — readings in a row that must show motion before alerting.
MOTION_MAX_CHANGE_RATIO = float(os.getenv("MOTION_MAX_CHANGE_RATIO", "0.5"))
MOTION_MAX_FOCUS_SHIFT  = float(os.getenv("MOTION_MAX_FOCUS_SHIFT", "1.6"))
MOTION_CONSEC_FRAMES    = int(os.getenv("MOTION_CONSEC_FRAMES", "3"))

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
# Shutdown signal — set by the post_shutdown hook so the motion thread can
# leave its loop and release the camera before the interpreter finalises.
#
# Letting the daemon thread simply be killed aborts the process: CPython stops
# it by unwinding out of whatever call it is in, and it is almost always inside
# an OpenCV call, which is C++. Unwinding through a C++ frame that has no
# handler reaches std::terminate, so the process dies with SIGABRT and
# "FATAL: exception not rethrown" instead of exiting cleanly.
# ---------------------------------------------------------------------------

_shutdown = threading.Event()
_motion_thread: threading.Thread | None = None

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
    while time.monotonic() - start < duration and not _shutdown.is_set():
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
# Focus control
#
# Resolved once per process and reapplied on every camera (re)open, so a
# mid-run reconnect never costs another sweep.
# ---------------------------------------------------------------------------

FOCUS_MIN, FOCUS_MAX   = 1, 1023
FOCUS_SETTLE_SECONDS   = 0.35  # time for the lens motor to reach a position
FOCUS_SETTLE_FRAMES    = 5     # buffered frames to discard after it moves

_locked_focus: int | None = None
_focus_resolved = False


def _focus_measure(gray) -> float:
    """
    Variance of the Laplacian — the standard focus measure. Higher means
    sharper. Must be given an unblurred grayscale frame; the 21x21 blur the
    motion detector applies would erase exactly the detail this reads.
    """
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def _sharpness(cap: "cv2.VideoCapture") -> float:
    """Focus measure of the next settled frame, discarding buffered ones."""
    for _ in range(FOCUS_SETTLE_FRAMES):
        cap.grab()
    ret, frame = cap.read()
    if not ret:
        return -1.0
    return _focus_measure(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))


def _autofocus_sweep(cap: "cv2.VideoCapture") -> int | None:
    """
    One-shot software autofocus: steps the lens across its range and keeps the
    position that produces the sharpest image.

    Freezing the sensor's own continuous autofocus would be simpler, but the
    UVC driver never reports the position it settles on — focus_absolute stays
    flagged `inactive` while autofocus is on — so the position has to be found
    here. Takes roughly ten seconds, once, at startup. Returns None if the
    camera rejects manual focus.
    """
    best_focus: int | None = None
    best_score = -1.0

    def scan(positions) -> bool:
        nonlocal best_focus, best_score
        for pos in positions:
            if _shutdown.is_set():
                break
            if not cap.set(cv2.CAP_PROP_FOCUS, pos):
                return False
            time.sleep(FOCUS_SETTLE_SECONDS)
            score = _sharpness(cap)
            logger.debug("focus %4d → sharpness %.1f", pos, score)
            if score > best_score:
                best_focus, best_score = pos, score
        return True

    logger.info("Running autofocus sweep — this takes a few seconds")
    coarse_step = (FOCUS_MAX - FOCUS_MIN) // 8
    if not scan(range(FOCUS_MIN, FOCUS_MAX + 1, coarse_step)) or best_focus is None:
        if not _shutdown.is_set():
            logger.warning("Camera rejected manual focus — leaving focus untouched")
        return None

    # Refine around the coarse winner.
    scan(range(
        max(best_focus - coarse_step, FOCUS_MIN),
        min(best_focus + coarse_step, FOCUS_MAX) + 1,
        max(coarse_step // 4, 1),
    ))

    logger.info("Autofocus sweep chose focus %d (sharpness %.1f)", best_focus, best_score)
    return best_focus


def _configure_camera(cap: "cv2.VideoCapture") -> None:
    """
    Pins the lens so the sensor stops refocusing on its own, which is what
    produces the whole-frame changes the motion detector misreads as movement.
    Safe to call again after a reconnect.
    """
    global _locked_focus, _focus_resolved

    if CAMERA_AUTOFOCUS:
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        logger.info("Continuous autofocus left ON (CAMERA_AUTOFOCUS=true)")
        return

    if not cap.set(cv2.CAP_PROP_AUTOFOCUS, 0):
        logger.warning("Camera does not support disabling autofocus")
        return
    logger.info("Continuous autofocus disabled")

    if not _focus_resolved:
        _locked_focus = CAMERA_FOCUS if CAMERA_FOCUS is not None else _autofocus_sweep(cap)
        _focus_resolved = True

    if _locked_focus is not None:
        cap.set(cv2.CAP_PROP_FOCUS, _locked_focus)
        # Let the motor arrive, then drop the frames the driver buffered while
        # it was still moving — otherwise the loop's first comparison is made
        # against a stale, differently-focused frame and trips its own filter.
        time.sleep(FOCUS_SETTLE_SECONDS)
        for _ in range(FOCUS_SETTLE_FRAMES):
            cap.grab()
        logger.info("Focus locked at %d", _locked_focus)


# ---------------------------------------------------------------------------
# Motion-detection thread
# ---------------------------------------------------------------------------

def _reference(frame):
    """
    The pair of values the next frame is compared against: the blurred
    grayscale image for differencing, and the focus measure (which needs the
    unblurred image) for spotting lens and lighting changes.
    """
    gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray_full, (21, 21), 0), _focus_measure(gray_full)


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
    _configure_camera(cap)

    ret, frame = cap.read()
    if not ret:
        logger.error("Failed to read first frame — aborting motion loop")
        cap.release()
        return

    _set_frame(frame)
    prev_gray, prev_sharpness = _reference(frame)
    last_alert: float = 0.0
    consecutive: int = 0  # readings in a row showing motion

    logger.info(
        "Motion detection active (min_area=%d px², cooldown=%d s, "
        "confirm=%d frames, max_change=%.0f%%, max_focus_shift=%.1fx)",
        MOTION_MIN_AREA, MOTION_COOLDOWN, MOTION_CONSEC_FRAMES,
        MOTION_MAX_CHANGE_RATIO * 100, MOTION_MAX_FOCUS_SHIFT,
    )

    while not _shutdown.is_set():
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

            if _shutdown.is_set():
                # Shutdown cut the recording short; the event loop is going
                # away, so there is nothing left to send the clip to.
                if path:
                    os.remove(path)
                break

            asyncio.run_coroutine_threadsafe(_send_clip(), loop)

            # Recording paused frame reads for VIDEO_DURATION seconds — the
            # previous grayscale reference is stale, so refresh it.
            consecutive = 0
            ret, frame = cap.read()
            if ret:
                _set_frame(frame)
                prev_gray, prev_sharpness = _reference(frame)
            continue

        if not _motion_enabled.is_set():
            # Detection is paused — keep reading frames so _latest_frame stays
            # fresh (for /photo) and to avoid stale prev_gray on resume.
            consecutive = 0
            ret, frame = cap.read()
            if ret:
                _set_frame(frame)
                prev_gray, prev_sharpness = _reference(frame)
            time.sleep(0.1)
            continue

        ret, frame = cap.read()
        if not ret:
            logger.warning("Frame read failed — reopening camera in 2 s")
            cap.release()
            time.sleep(2.0)
            cap = cv2.VideoCapture(CAMERA_INDEX)
            _configure_camera(cap)
            continue

        _set_frame(frame)

        # Frame-difference motion detection
        gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharpness = _focus_measure(gray_full)
        gray   = cv2.GaussianBlur(gray_full, (21, 21), 0)
        delta  = cv2.absdiff(prev_gray, gray)
        thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        prev_gray = gray

        # How far sharpness swung since the last reading, as a ratio ≥ 1.
        focus_shift = (
            max(sharpness, prev_sharpness) / max(min(sharpness, prev_sharpness), 1e-6)
        )
        prev_sharpness = sharpness

        changed_ratio = cv2.countNonZero(thresh) / thresh.size
        if focus_shift > MOTION_MAX_FOCUS_SHIFT:
            # The whole image got sharper or blurrier at once. Objects move
            # through the frame without changing how well it is focused, so
            # this is the lens or the lighting, not something in the scene.
            logger.info(
                "Ignoring %.1fx sharpness jump (lens or lighting change) — not motion",
                focus_shift,
            )
            consecutive = 0
        elif changed_ratio > MOTION_MAX_CHANGE_RATIO:
            # Most of the frame changed in a single step. Real movement is
            # localised, so this is a global event — an exposure or
            # white-balance correction, a light being switched on.
            logger.info(
                "Ignoring global frame change (%.0f%% of pixels) — not motion",
                changed_ratio * 100,
            )
            consecutive = 0
        elif any(cv2.contourArea(c) > MOTION_MIN_AREA for c in contours):
            consecutive += 1
        else:
            consecutive = 0

        now = time.monotonic()
        if (
            consecutive >= MOTION_CONSEC_FRAMES
            and now - last_alert >= MOTION_COOLDOWN
        ):
            last_alert = now
            consecutive = 0
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

            if _shutdown.is_set():
                if path:
                    os.remove(path)
                break

            asyncio.run_coroutine_threadsafe(_alert(), loop)

            # Recording consumed VIDEO_DURATION seconds of frames, so the
            # grayscale reference is stale — refresh it before the next diff.
            ret, frame = cap.read()
            if ret:
                _set_frame(frame)
                prev_gray, prev_sharpness = _reference(frame)

        time.sleep(0.1)  # ~10 fps polling rate

    cap.release()
    logger.info("Motion-detection thread stopped, camera released")


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


async def cmd_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches commands from everyone outside ALLOWED_USERS. Logged at WARNING
    with the sender's identity, since repeated hits are worth noticing on a
    camera that watches a home.
    """
    user = update.effective_user
    logger.warning(
        "Rejected %s from unauthorised user %s (@%s, %s)",
        update.message.text.split()[0] if update.message and update.message.text else "command",
        user.id if user else "unknown",
        user.username if user else "unknown",
        user.full_name if user else "unknown",
    )
    if update.message:
        await update.message.reply_text("Not authorised.")


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------

async def _post_init(application: Application) -> None:
    """
    Called by python-telegram-bot after the app initialises but before
    polling begins. The running event loop is already available here, so
    this is the correct place to start the motion-detection thread.
    """
    global _motion_thread

    loop = asyncio.get_running_loop()
    # Still a daemon: if the join in _post_shutdown ever times out, the process
    # must be able to exit anyway rather than hang.
    _motion_thread = threading.Thread(
        target=motion_detection_loop,
        args=(application.bot, loop),
        name="motion-detection",
        daemon=True,
    )
    _motion_thread.start()
    logger.info("Motion-detection thread started")

    commands = [
        BotCommand("photo",          "Capture a photo on demand"),
        BotCommand("video",          "Capture a short video on demand"),
        BotCommand("motion_on",      "Enable motion detection"),
        BotCommand("motion_off",     "Disable motion detection"),
        BotCommand("motion_status",  "Show motion detection state"),
    ]
    # Advertise the menu only to the people allowed to use it; everyone else
    # gets an empty command list rather than a description of the camera.
    await application.bot.set_my_commands([])
    for user_id in sorted(ALLOWED_USERS):
        try:
            await application.bot.set_my_commands(
                commands, scope=BotCommandScopeChat(chat_id=user_id)
            )
        except Exception as exc:
            # Telegram rejects the scope until the user has opened a chat
            # with the bot; the commands still work, they are just unlisted.
            logger.warning("Could not publish command list to user %s: %s", user_id, exc)

    logger.info("Commands restricted to %d authorised user(s)", len(ALLOWED_USERS))


async def _post_shutdown(application: Application) -> None:
    """
    Called by python-telegram-bot once polling has stopped, while the
    interpreter is still healthy. Stops the motion thread here so it is never
    killed mid-OpenCV-call during finalisation, which aborts the process.
    """
    if _motion_thread is None:
        return

    _shutdown.set()
    # A recording in progress is the slow case; _record_clip checks the same
    # flag, so the thread returns shortly after its current camera read.
    _motion_thread.join(timeout=VIDEO_DURATION + 3)
    if _motion_thread.is_alive():
        logger.warning("Motion-detection thread did not stop in time")


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    authorised = filters.User(user_id=ALLOWED_USERS)
    app.add_handler(CommandHandler("start",          cmd_start,         filters=authorised))
    app.add_handler(CommandHandler("photo",          cmd_photo,         filters=authorised))
    app.add_handler(CommandHandler("video",          cmd_video,         filters=authorised))
    app.add_handler(CommandHandler("motion_on",      cmd_motion_on,     filters=authorised))
    app.add_handler(CommandHandler("motion_off",     cmd_motion_off,    filters=authorised))
    app.add_handler(CommandHandler("motion_status",  cmd_motion_status, filters=authorised))
    # Registered last so it only sees commands the handlers above declined.
    app.add_handler(MessageHandler(filters.COMMAND & ~authorised, cmd_denied))

    logger.info("Starting camera bot")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
