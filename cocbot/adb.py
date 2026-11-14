"""ADB device interface — screencap, fast tap via dd, swipe, pinch."""

from __future__ import annotations

import logging
import random
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

EV_DEV = "/dev/input/event4"
MAX_X = 32767
MAX_Y = 32767
SCREEN_W = 1280
SCREEN_H = 720

# Linux input event types / codes
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0x00
ABS_MT_SLOT = 0x2F
ABS_MT_TRACKING_ID = 0x39
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
BTN_TOUCH = 0x14A


class AdbDevice:
    """Talks to BlueStacks (or any Android) via ADB subprocess calls."""

    def __init__(self, serial: str = "127.0.0.1:5555") -> None:
        self.serial = serial
        self._connected = False
        self.actual_w = SCREEN_W
        self.actual_h = SCREEN_H

    def _query_display_size(self) -> None:
        """Query physical screen resolution from device via wm size."""
        import re
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.findall(r"(\d+)x(\d+)", r.stdout)
            if m:
                w, h = int(m[-1][0]), int(m[-1][1])
                if h > w:
                    w, h = h, w
                self.actual_w = w
                self.actual_h = h
                log.info("Device display resolution: %dx%d (canonical: %dx%d)", w, h, SCREEN_W, SCREEN_H)
        except Exception:
            pass
    # ── connection ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Run ``adb connect`` and ensure device state is healthy ('device' not 'offline')."""
        try:
            # Cek status saat ini
            r_st = subprocess.run(
                ["adb", "-s", self.serial, "get-state"],
                capture_output=True, text=True, timeout=5,
            )
            state = r_st.stdout.strip()
            if state != "device":
                # Disconnect sesi stale/offline terlebih dahulu
                subprocess.run(
                    ["adb", "disconnect", self.serial],
                    capture_output=True, text=True, timeout=5,
                )
                time.sleep(0.3)
                subprocess.run(
                    ["adb", "connect", self.serial],
                    capture_output=True, text=True, timeout=10,
                )

            r_chk = subprocess.run(
                ["adb", "-s", self.serial, "get-state"],
                capture_output=True, text=True, timeout=5,
            )
            ok = r_chk.stdout.strip() == "device"
            self._connected = ok
            if ok:
                self._query_display_size()
            else:
                log.warning("adb connect failed or device offline: state=%s", r_chk.stdout.strip())
            return ok
        except Exception:
            log.exception("adb connect error")
            self._connected = False
            return False
    def is_connected(self) -> bool:
        """Check device state via ``adb get-state``."""
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "get-state"],
                capture_output=True, text=True, timeout=5,
            )
            ok = r.stdout.strip() == "device"
            self._connected = ok
            return ok
        except Exception:
            self._connected = False

    def is_emulator_running(self) -> bool:
        """Check if BlueStacks HD-Player process is running in Windows."""
        try:
            r = subprocess.run(
                ["powershell", "-Command", "Get-Process -Name 'HD-Player' -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=5,
            )
            return bool(r.stdout.strip())
        except Exception:
            return False

    def launch_emulator(self) -> bool:
        """Launch BlueStacks 5 (Pie64 instance) and wait for ADB port 5555."""
        import socket
        hd_paths = [
            Path(r"C:\Program Files\BlueStacks_nxt\HD-Player.exe"),
            Path(r"C:\Program Files (x86)\BlueStacks_nxt\HD-Player.exe"),
        ]
        exe = next((p for p in hd_paths if p.exists()), None)
        if exe is None:
            log.error("HD-Player.exe not found on system!")
            return False

        log.info("[AUTO-LAUNCH] Membuka BlueStacks (Pie64)...")
        try:
            subprocess.Popen([str(exe), "--instance", "Pie64"])
        except Exception:
            log.exception("Gagal membuka HD-Player.exe")
            return False

        # Poll port 5555 using quick socket connect (max 45 seconds)
        log.info("[AUTO-LAUNCH] Menunggu emulator booting & port ADB 5555 aktif...")
        port = 5555
        if ":" in self.serial:
            try:
                port = int(self.serial.split(":")[-1])
            except Exception:
                port = 5555

        for _ in range(35):
            time.sleep(1.2)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.8):
                    log.info("[AUTO-LAUNCH] Port ADB %d terbuka! Menyambungkan ADB...", port)
                    time.sleep(1.0)
                    return self.connect()
            except (socket.timeout, ConnectionRefusedError, OSError):
                pass
        return False

    def is_game_running(self) -> bool:
        """Check if Clash of Clans process is running in Android."""
        if not self.is_connected():
            self.connect()
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", "pidof", "com.supercell.clashofclans"],
                capture_output=True, text=True, timeout=5,
            )
            return bool(r.stdout.strip())
        except Exception:
            return False

    def launch_game(self) -> bool:
        """Launch Clash of Clans via am start."""
        if not self.is_connected():
            self.connect()
        try:
            r = subprocess.run(
                [
                    "adb", "-s", self.serial, "shell", "am", "start", "-n",
                    "com.supercell.clashofclans/com.supercell.titan.GameApp",
                ],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def ensure_game_running(self) -> bool:
        """Auto-reopen BlueStacks if closed/crashed, and auto-launch Clash of Clans."""
        # 1. Ensure emulator is running
        if not self.is_emulator_running():
            log.warning("[AUTO-RECOVERY] Emulator BlueStacks tidak berjalan! Meluncurkan otomatis...")
            if not self.launch_emulator():
                return False
            time.sleep(3.0)

        # 2. Ensure ADB connection is active
        if not self.is_connected():
            log.info("[AUTO-RECOVERY] Menghubungkan ulang ADB...")
            self.connect()

        # 3. Ensure Clash of Clans is running
        if not self.is_game_running():
            log.warning("[AUTO-RECOVERY] Game CoC tidak berjalan/crash! Membuka game otomatis...")
            self.launch_game()
            time.sleep(8.0)
            return self.is_game_running()

        return True

    def screenshot(self) -> np.ndarray | None:
        """Capture screen as BGR ndarray via PNG pipe (~650 ms), with auto-retry."""
        for attempt in range(3):
            try:
                r = subprocess.run(
                    ["adb", "-s", self.serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=25,
                )
                if r.returncode == 0 and r.stdout:
                    buf = np.frombuffer(r.stdout, dtype=np.uint8)
                    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                    if img is not None:
                        orig_h, orig_w = img.shape[:2]
                        if (orig_w, orig_h) != (SCREEN_W, SCREEN_H):
                            self.actual_w = orig_w
                            self.actual_h = orig_h
                            img = cv2.resize(img, (SCREEN_W, SCREEN_H), interpolation=cv2.INTER_AREA)
                        return img
                log.warning("screencap -p failed (attempt=%d, rc=%d)", attempt + 1, r.returncode)
                if attempt < 2:
                    self.connect()
                    time.sleep(0.5)
            except Exception:
                log.exception("screenshot error")
                if attempt < 2:
                    time.sleep(0.5)
        return None

    def screenshot_raw(self) -> np.ndarray | None:
        """Capture screen as BGR ndarray via raw RGBX pipe (~360 ms).

        Raw screencap header: 4 × uint32-LE (width, height, pixel_format, 0).
        Pixel data is RGBX (4 bytes/pixel).
        """
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "exec-out", "screencap"],
                capture_output=True, timeout=15,
            )
            if r.returncode != 0 or len(r.stdout) < 16:
                log.warning("screencap raw failed (rc=%d, len=%d)",
                            r.returncode, len(r.stdout))
                return None
            hdr = struct.unpack_from("<IIII", r.stdout, 0)
            w, h = int(hdr[0]), int(hdr[1])
            expected = 16 + w * h * 4
            if len(r.stdout) < expected:
                log.warning("raw screencap truncated: got %d, need %d",
                            len(r.stdout), expected)
                return None
            pixels = np.frombuffer(r.stdout, dtype=np.uint8, offset=16)
            rgbx = pixels.reshape((h, w, 4))
            bgr = cv2.cvtColor(rgbx, cv2.COLOR_RGBA2BGR)
            return bgr
        except Exception:
            log.exception("screenshot_raw error")
            return None

    # ── input helpers ────────────────────────────────────────────────────

    @staticmethod
    def _ev(etype: int, code: int, value: int) -> bytes:
        """Pack a single ``input_event`` (24-byte: tv_sec, tv_usec, type, code, value)."""
        return struct.pack("<qqHHi", 0, 0, etype, code, value)

    def _push_and_dd(self, data: bytes, filename: str = "tap.bin") -> bool:
        """Write *data* to a local temp file, push to device, and dd into event device."""
        tmp = Path(tempfile.gettempdir()) / filename
        tmp.write_bytes(data)
        remote = f"/data/local/tmp/{filename}"
        try:
            subprocess.run(
                ["adb", "-s", self.serial, "push", str(tmp), remote],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["adb", "-s", self.serial, "shell",
                 f"dd if={remote} of={EV_DEV} bs={len(data)}"],
                capture_output=True, timeout=10,
            )
            return True
        except Exception:
            log.exception("_push_and_dd error")
            return False
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _scale(px: int, screen: int) -> int:
        """Convert pixel coordinate to input-event absolute coordinate."""
        return px * MAX_X // screen

    # ── taps ─────────────────────────────────────────────────────────────

    def tap(self, x: int, y: int, jitter: int = 0) -> bool:
        """Tap at canonical (x, y) coordinates (auto-scaled to physical resolution)."""
        if jitter > 0:
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)
        x = max(0, min(x, SCREEN_W - 1))
        y = max(0, min(y, SCREEN_H - 1))

        act_x = int(x * self.actual_w / SCREEN_W)
        act_y = int(y * self.actual_h / SCREEN_H)
        act_x = max(0, min(act_x, self.actual_w - 1))
        act_y = max(0, min(act_y, self.actual_h - 1))
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", "input", "tap", str(act_x), str(act_y)],
                capture_output=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            log.exception("tap error")
            return False

    def tap_batch(self, points: list[tuple[int, int]], jitter: int = 0) -> bool:
        """Fire many taps in a single ADB shell call (auto-scaled to physical resolution)."""
        if not points:
            return True
        cmds: list[str] = []
        for x, y in points:
            if jitter > 0:
                x += random.randint(-jitter, jitter)
                y += random.randint(-jitter, jitter)
            x = max(0, min(x, SCREEN_W - 1))
            y = max(0, min(y, SCREEN_H - 1))
            act_x = int(x * self.actual_w / SCREEN_W)
            act_y = int(y * self.actual_h / SCREEN_H)
            cmds.append(f"input tap {act_x} {act_y}")
        shell = "; ".join(cmds)
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", shell],
                capture_output=True,
                timeout=max(15, len(points) * 2),
            )
            return r.returncode == 0
        except Exception:
            log.exception("tap_batch error")
            return False

    def deploy_swipes(
        self,
        lines: list[tuple[int, int, int, int]],
        duration_ms: int = 3000,
    ) -> bool:
        """Swipe along multiple deployment lines in parallel via ``input swipe``.

        Each *line* is ``(x1, y1, x2, y2)``.  All swipes run concurrently
        on-device, so the total time equals one *duration_ms* period.
        COC deploys one troop per frame (~30 fps) while a finger is held,
        so a 3-second swipe deploys ~90 troops along one line.
        """
        if not lines:
            return True
        cmds = [
            f"input swipe {int(x1*self.actual_w/SCREEN_W)} {int(y1*self.actual_h/SCREEN_H)} {int(x2*self.actual_w/SCREEN_W)} {int(y2*self.actual_h/SCREEN_H)} {duration_ms}"
            for x1, y1, x2, y2 in lines
        ]
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", shell],
                capture_output=True,
                timeout=max(15, duration_ms // 1000 + 5),
            )
            return r.returncode == 0
        except Exception:
            log.exception("deploy_swipes error")
            return False

    # ── swipe ────────────────────────────────────────────────────────────

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 350, steps: int = 10) -> bool:
        """Swipe from (x1,y1) to (x2,y2) via ``adb shell input swipe``."""
        ax1 = int(x1 * self.actual_w / SCREEN_W)
        ay1 = int(y1 * self.actual_h / SCREEN_H)
        ax2 = int(x2 * self.actual_w / SCREEN_W)
        ay2 = int(y2 * self.actual_h / SCREEN_H)
        try:
            r = subprocess.run(
                [
                    "adb", "-s", self.serial, "shell", "input", "swipe",
                    str(ax1), str(ay1), str(ax2), str(ay2), str(duration_ms),
                ],
                capture_output=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            log.exception("swipe error")
            return False

    def hold(self, x: int, y: int, duration_ms: int = 1500) -> bool:
        """Continuous touch-and-hold at (x, y) for duration_ms.

        In COC, holding down on the battlefield deploys troops continuously
        at ~35 units/second until the troop card is completely empty.
        """
        ax = int(x * self.actual_w / SCREEN_W)
        ay = int(y * self.actual_h / SCREEN_H)
        ax = max(0, min(ax, self.actual_w - 1))
        ay = max(0, min(ay, self.actual_h - 1))
        try:
            r = subprocess.run(
                [
                    "adb", "-s", self.serial, "shell", "input", "swipe",
                    str(ax), str(ay), str(ax), str(ay), str(duration_ms),
                ],
                capture_output=True,
                timeout=max(10, duration_ms // 1000 + 5),
            )
            return r.returncode == 0
        except Exception:
            log.exception("hold error")
            return False
    # ── pinch ────────────────────────────────────────────────────────────

    def pinch(self, cx: int, cy: int,
              start_dist: int, end_dist: int, steps: int = 10) -> bool:
        """Two-finger pinch centered on (cx, cy).

        *start_dist* / *end_dist* are half-distances between the two fingers
        (in screen pixels). ``start > end`` = pinch-in (zoom out).
        """
        parts: list[bytes] = []

        def _fingers_down(d: int, first: bool = True) -> None:
            lx = max(0, cx - d)
            rx = min(SCREEN_W - 1, cx + d)
            # slot 0
            parts.append(self._ev(EV_ABS, ABS_MT_SLOT, 0))
            if first:
                parts.append(self._ev(EV_ABS, ABS_MT_TRACKING_ID, 80))
            parts.append(self._ev(EV_ABS, ABS_MT_POSITION_X, self._scale(lx, SCREEN_W)))
            parts.append(self._ev(EV_ABS, ABS_MT_POSITION_Y, self._scale(cy, SCREEN_H)))
            # slot 1
            parts.append(self._ev(EV_ABS, ABS_MT_SLOT, 1))
            if first:
                parts.append(self._ev(EV_ABS, ABS_MT_TRACKING_ID, 81))
            parts.append(self._ev(EV_ABS, ABS_MT_POSITION_X, self._scale(rx, SCREEN_W)))
            parts.append(self._ev(EV_ABS, ABS_MT_POSITION_Y, self._scale(cy, SCREEN_H)))
            if first:
                parts.append(self._ev(EV_KEY, BTN_TOUCH, 1))
            parts.append(self._ev(EV_SYN, SYN_REPORT, 0))

        # initial touch
        _fingers_down(start_dist, first=True)

        # intermediate steps
        for i in range(1, steps + 1):
            t = i / steps
            d = int(start_dist + (end_dist - start_dist) * t)
            _fingers_down(d, first=False)

        # release both fingers
        parts.append(self._ev(EV_ABS, ABS_MT_SLOT, 0))
        parts.append(self._ev(EV_ABS, ABS_MT_TRACKING_ID, -1))
        parts.append(self._ev(EV_ABS, ABS_MT_SLOT, 1))
        parts.append(self._ev(EV_ABS, ABS_MT_TRACKING_ID, -1))
        parts.append(self._ev(EV_KEY, BTN_TOUCH, 0))
        parts.append(self._ev(EV_SYN, SYN_REPORT, 0))

        return self._push_and_dd(b"".join(parts), "pinch.bin")

    # ── misc ─────────────────────────────────────────────────────────────

    def back(self) -> bool:
        """Press the Android back button."""
        try:
            subprocess.run(
                ["adb", "-s", self.serial, "shell", "input", "keyevent", "4"],
                capture_output=True, timeout=10,
            )
            return True
        except Exception:
            log.exception("back key error")
            return False
