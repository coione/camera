# Raspberry Pi Camera Telegram Bot

A Python service that watches a USB camera for motion and sends a short video clip to Telegram on each event. Responds to `/photo` and `/video` for on-demand captures, and to `/motion_on`, `/motion_off`, `/motion_status` to control detection at runtime.

## Prerequisites

- Raspberry Pi OS (Bullseye or Bookworm)
- USB camera visible at `/dev/video0` (or set `CAMERA_INDEX`)
- Python 3.10+
- A Telegram bot token — create one with [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID — get it from [@userinfobot](https://t.me/userinfobot)

## Setup

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3-venv libglib2.0-0 ffmpeg v4l-utils
```

`ffmpeg` is required to encode motion clips and `/video` captures as H.264/MP4 so Telegram clients can play them inline.

### 2. Create a virtual environment and install packages

```bash
cd /home/pi/Projects/camera
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
nano .env    # set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

### 4. Test manually

```bash
venv/bin/python camera_bot.py
```

Send `/start` to your bot in Telegram to confirm it responds, then `/photo` to test a still capture and `/video` to test a clip. Moving in front of the camera should also trigger an automatic motion-detection clip.

### 5. Install as a systemd service

```bash
sudo cp camera-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable camera-bot
sudo systemctl start camera-bot
```

Check status and follow logs:

```bash
sudo systemctl status camera-bot
journalctl -u camera-bot -f
```

## Configuration

All settings are controlled via environment variables (`.env` file):

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **required** | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | **required** | Target chat or user ID for alerts |
| `TELEGRAM_ALLOWED_USERS` | *(`TELEGRAM_CHAT_ID`)* | Comma-separated user IDs allowed to issue commands |
| `CAMERA_INDEX` | `0` | OpenCV camera index (`0` = first USB camera) |
| `MOTION_MIN_AREA` | `3000` | Minimum contour area in pixels² to trigger an alert |
| `MOTION_COOLDOWN` | `10` | Minimum seconds between consecutive motion alerts |
| `VIDEO_DURATION` | `5` | Length in seconds of motion clips and `/video` captures |
| `CAMERA_AUTOFOCUS` | `false` | Leave the sensor's continuous autofocus on (causes false alerts) |
| `CAMERA_FOCUS` | *(sweep)* | Fixed focus position `1`–`1023`; unset runs a one-shot sweep at startup |
| `MOTION_MAX_FOCUS_SHIFT` | `1.6` | Reject a reading when image sharpness swings by more than this factor |
| `MOTION_MAX_CHANGE_RATIO` | `0.5` | Reject a reading when more than this fraction of the frame changes at once |
| `MOTION_CONSEC_FRAMES` | `3` | Readings in a row (~0.1 s each) that must show motion before alerting |
| `MOTION_THRESHOLD_MAX` | `25` | Upper bound on the per-pixel difference that counts as changed |
| `MOTION_THRESHOLD_MIN` | `6` | Lower bound on that threshold, however quiet the camera is |
| `MOTION_NOISE_MARGIN` | `4.0` | Threshold is this many times the measured noise peak |
| `CAMERA_LOW_LIGHT` | `true` | Let the sensor trade frame rate for exposure time in the dark |

Increase `MOTION_MIN_AREA` to reduce false positives from lighting changes. Increase `MOTION_COOLDOWN` to limit alert frequency. Note that each alert now records for `VIDEO_DURATION` seconds, during which no new motion is detected, so the effective gap between alerts is roughly `MOTION_COOLDOWN` + `VIDEO_DURATION`.

### Access control

A Telegram bot accepts messages from anyone who learns its username, so commands are restricted to an allowlist. Without it a stranger could run `/photo` or `/video` and watch the room, or `/motion_off` and silence the alarm.

`TELEGRAM_ALLOWED_USERS` holds the numeric user IDs permitted to issue commands. It defaults to `TELEGRAM_CHAT_ID`, which for a private chat is also the owner's user ID, so a single-user setup needs no extra configuration. If `TELEGRAM_CHAT_ID` is a group (a negative ID), it is not a user ID and the allowlist must be set explicitly — the bot refuses to start otherwise rather than fall back to something permissive.

Commands from anyone else are rejected with a short reply and logged at `WARNING` with the sender's ID, username, and name, so repeated attempts show up in `journalctl -u camera-bot`. The command menu is published only to allowed users; everyone else sees an empty list instead of a description of the camera. Note that this restricts commands, not alerts — motion clips always go to `TELEGRAM_CHAT_ID`.

### Low light

Darkness compresses contrast: a person at night differs from the background by a few grey levels rather than tens, so a detector tuned for daylight simply stops seeing anything. Two things address that.

The sensor is allowed to lengthen its exposure when light is scarce (`CAMERA_LOW_LIGHT`, on by default), trading frame rate for the light it needs. This is a UVC control with no OpenCV property, so it is applied through `v4l2-ctl`; if that is missing the bot logs a warning and carries on.

More importantly, the per-pixel difference that counts as "changed" is no longer fixed. It was hard-coded at 25, while a static scene measures a frame-to-frame noise peak of about 3 grey levels — roughly eight times more margin than the noise called for. Since sensitivity tracks the threshold one for one, that directly set the faintest detectable object at 25 levels of contrast. The threshold is now `MOTION_NOISE_MARGIN` times the noise the camera is actually producing, measured only on frames where nothing is happening so that real movement never inflates it, and clamped between `MOTION_THRESHOLD_MIN` and `MOTION_THRESHOLD_MAX`. The estimate rises quickly and decays slowly, so a sensor that gets noisy raises the bar at once but one quiet moment cannot make the detector jumpy.

In practice the threshold settles around 9 on a quiet camera — about 2.8x more sensitive than before — and climbs back towards the ceiling under heavy noise. Because `MOTION_THRESHOLD_MAX` is the old fixed value, detection can never end up less sensitive than it was. `journalctl -u camera-bot` reports every change as `Pixel threshold now N (noise peak X, scene brightness Y)`, which is the quickest way to see what the camera is doing after dark.

### Focus and false positives

Continuous autofocus is a major source of false motion alerts: each refocus takes the whole frame from blurry to sharp, which frame-difference analysis reads as a large moving object. A fixed camera never needs to refocus, so autofocus is disabled at startup and the lens is locked.

Because the UVC driver never reports the position its own autofocus settles on, the bot finds one itself: a coarse-to-fine sweep of the focus range, keeping the sharpest position. This takes about ten seconds, once, at startup — the log line `Autofocus sweep chose focus N` reports the result. Set `CAMERA_FOCUS=N` to skip the sweep on later starts, and re-run it (by unsetting it and restarting) whenever the camera is moved or re-aimed.

Three filters then guard against whatever global changes remain — an auto-exposure step, a light being switched on. The one that matters for focus is `MOTION_MAX_FOCUS_SHIFT`: objects move through a frame without changing how well it is focused, so a sharpness swing means the lens or the lighting, not the scene. On the reference camera a static scene holds `1.01x` and a large moving object `1.19x`, while a refocus spikes to `6.2x`. Lower the threshold towards `1.3` if refocus alerts still slip through; raise it if genuine motion is being suppressed.

## Commands

| Command | Action |
|---|---|
| `/start` | Show the command list |
| `/photo` | Capture a still photo on demand |
| `/video` | Capture a `VIDEO_DURATION`-second clip on demand |
| `/motion_on` | Enable motion detection |
| `/motion_off` | Disable motion detection |
| `/motion_status` | Show whether motion detection is on or off |

## Troubleshooting

**Camera not found** — Run `ls /dev/video*` to list devices and set `CAMERA_INDEX` accordingly.

**Permission denied on `/dev/video0`** — The service runs with `Group=video`. For manual runs, add your user: `sudo usermod -aG video pi` (log out and back in).

**Bot not responding** — Confirm `TELEGRAM_BOT_TOKEN` is correct and no other bot instance is already polling with the same token.

**High CPU usage** — The motion loop runs at ~10 fps. Increase the `time.sleep` in `motion_detection_loop` or lower camera resolution via `cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)`.
