"""Screen navigation and popup dismissal."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from cocbot.adb import AdbDevice
    from cocbot.vision import ScreenType, Vision

log = logging.getLogger("cocbot")

# Layout coordinates (1280x720)
_BTN = {
    "attack":     (95, 650),
    "find_match": (290, 515),
    "train_army": (1131, 642),
    "next":       (1166, 512),
    "end_battle": (88, 525),
    "build":      (1195, 650),
    "shop":       (1195, 390),
}

# Common popup-close regions
_POPUP_CLOSE_X = (1230, 55)        # generic X in top-right of modal
_OFFER_CLOSE_X = (1230, 55)         # offer/promo X (top-right red button)
_RELOAD_BTN    = (420, 428)        # "Reload" or "MUAT ULANG GAME" button
_SCREEN_CENTER = (640, 360)


class Navigator:
    """Navigate between COC screens and dismiss popups/interrupts."""

    def __init__(self, device: AdbDevice, vision: Vision) -> None:
        self.device = device
        self.vision = vision

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def go_home(self) -> bool:
        """Press back / dismiss popups until HOME screen detected (max 10 tries)."""
        from cocbot.vision import ScreenType

        for attempt in range(12):
            # Cek apakah emulator atau CoC tertutup/crash -> auto-launch otomatis
            if hasattr(self.device, "ensure_game_running"):
                if not self.device.ensure_game_running():
                    self._sleep(3000, 5000)
                    continue

            img = self.device.screenshot()
            screen = self.vision.detect_screen(img)
            log.debug("go_home attempt %d: screen=%s", attempt, screen)

            if screen == ScreenType.LOADING:
                log.info("[NAVIGASI] Layar transisi Memuat... Menunggu desa selesai di-load...")
                self._sleep(4000, 6000)
                continue
            if screen == ScreenType.HOME:
                # Deselect any selected building or wall so map is clean for next action
                if self.vision.has_text(img, ["tingkatkan", "info", "pilih baris"]):
                    self.device.tap(200, 50, jitter=4)
                    self._sleep(300, 500)
                return True
            if screen == ScreenType.BUILD_SUGGEST:
                log.info("[NAVIGASI] Menutup panel saran upgrade...")
                self.device.tap(300, 360, jitter=5)
                self._sleep(500, 800)
                continue
            # If on results screen ("Ke Beranda" / "Kembali")
            if self.vision.has_text(img, ["beranda", "kembali", "pulang"]):
                log.info("[NAVIGASI] Layar Hasil Pertempuran terdeteksi -> Menekan 'Ke Beranda'...")
                btn = self.vision.find_button(img, "beranda") or self.vision.find_button(img, "kembali")
                self.device.tap(*(btn or (640, 615)), jitter=5)
                # Jeda 6-8 detik agar CoC dan GPU emulator menyelesaikan alokasi aset desa tanpa terganggu screencap
                self._sleep(6000, 8000)
                continue

            # If on battle or search screen, press back doesn't work — must end battle
            if screen in (ScreenType.BATTLE, ScreenType.SEARCH):
                log.info("[NAVIGASI] Masih di dalam pertempuran -> Menekan 'Akhiri Serangan' / 'Menyerah'...")
                self.device.tap(70, 530, jitter=3)  # Akhiri Serangan / Menyerah
                self._sleep(1000, 1800)
                img_conf = self.device.screenshot()
                btn = self.vision.find_button(img_conf, "oke") or self.vision.find_button(img_conf, "ya")
                if btn:
                    self.device.tap(*btn, jitter=3)
                    self._sleep(1500, 2500)
                continue

            # If on Attack Menu, tap outside modal on right-side village grass (1000, 300)
            if screen == ScreenType.ATTACK_MENU:
                log.info("[NAVIGASI] Menutup menu Pertempuran (tap luar modal)...")
                self.device.tap(1000, 300, jitter=5)
                self._sleep(600, 1000)
                continue

            # If on Army Prep, tap close X at top-right (1235, 35)
            if screen == ScreenType.ARMY_PREP:
                log.info("[NAVIGASI] Menutup layar Persiapan Pasukan...")
                self.device.tap(1236, 65, jitter=3)
                self._sleep(800, 1500)
                continue

            # Try dismissing popups first
            if self.dismiss_popups(img):
                self._sleep(800, 1500)
                continue

            # Deselect any selected building/wall and close modals cleanly via empty grass & close X
            # (NEVER call device.back() on village screens as Android back triggers 'Ingin keluar dari game?')
            self.device.tap(200, 50, jitter=4)
            self._sleep(400, 600)
            self.device.tap(1235, 35, jitter=3)
            self._sleep(600, 1000)
        img = self.device.screenshot()
        return self.vision.detect_screen(img) == ScreenType.HOME

    def wait_for_screen(
        self,
        target: ScreenType,
        timeout: float = 30,
    ) -> bool:
        """Poll every ~2s until *target* screen is detected or timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            img = self.device.screenshot()
            if self.vision.detect_screen(img) == target:
                return True
            # Also dismiss popups while waiting
            self.dismiss_popups(img)
            self._sleep(1800, 2400)
        return False

    def dismiss_popups(self, img: np.ndarray) -> bool:
        """Detect and dismiss known popups. Returns True if a popup was handled."""
        from cocbot.vision import ScreenType
        screen = self.vision.detect_screen(img)
        if screen in (ScreenType.BATTLE, ScreenType.SEARCH):
            return False

        # Connection lost / network error
        if self.vision.has_text(img, ["koneksi", "connection", "terputus", "ulang game", "tidak aktif"]):
            log.info("Popup: connection lost – tapping reload")
            btn_reload = self.vision.find_button(img, "ulang") or _RELOAD_BTN
            self.device.tap(*btn_reload, jitter=4)
            self._sleep(4000, 6000)
            return True
        # Another device
        if self.vision.has_text(img, ["lain", "perangkat", "another"]):
            log.info("Popup: another device – waiting then reload")
            self._sleep(5000, 8000)
            self.device.tap(*_RELOAD_BTN, jitter=4)
            self._sleep(3000, 5000)
            return True

        # Blocked upgrade / TH requirement modal ("Perlu meningkatkan Balai Kota...")
        if self.vision.has_text(img, ["perlu", "perhatian", "balai kota ke level"]):
            log.info("Popup: TH requirement modal – dismissing via tap outside")
            self.device.tap(200, 50, jitter=3)
            self._sleep(500, 800)
            return True

        # Wall row upgrade confirm dialog ("Yakin ingin meningkatkan tembok... Batal / Oke")
        if self.vision.has_text(img, ["yakin", "meningkatkan tembok"]):
            log.info("Popup: wall upgrade confirmation – tapping Oke")
            btn_oke = self.vision.find_button(img, "oke")
            self.device.tap(*(btn_oke or (775, 465)), jitter=3)
            self._sleep(600, 1000)
            return True

        # Exit game confirmation ("Ingin keluar dari game? Batal / Oke")
        if self.vision.has_text(img, ["keluar", "batal"]):
            log.info("Popup: exit confirmation – tapping Batal")
            btn = self.vision.find_button(img, "batal")
            self.device.tap(*(btn or (502, 459)), jitter=3)
            self._sleep(500, 900)
            return True

        # Rate / review app popup
        if self.vision.has_text(img, ["nilai", "ulas", "review", "rate"]):
            log.info("Popup: rate app – tapping 'Nanti'")
            btn = self.vision.find_button(img, "nanti")
            if btn is None:
                btn = self.vision.find_button(img, "jangan")
            self.device.tap(*(btn or _SCREEN_CENTER), jitter=3)
            self._sleep(500, 900)
            return True

        # Star bonus / reward popup ("Oke" to dismiss)
        if self.vision.has_text(img, ["bonus", "bintang", "diterima"]):
            log.info("Popup: star bonus – tapping Oke")
            btn = self.vision.find_button(img, "oke")
            self.device.tap(*(btn or _SCREEN_CENTER), jitter=3)
            self._sleep(500, 900)
            return True

        # Promotional offers & skin popups
        if self.vision.has_text(img, ["penawaran", "offer", "spesial", "special", "paket", "adipati", "titan"]):
            log.info("Popup: offer / promo – closing via (1230, 55)")
            self.device.tap(*_OFFER_CLOSE_X, jitter=3)
            self._sleep(600, 1000)
            return True

        # Clan war / league / event banners
        if self.vision.has_text(img, ["perang", "liga", "event"]):
            log.info("Popup: war/league/event banner – closing")
            self.device.tap(*_POPUP_CLOSE_X, jitter=3)
            self._sleep(500, 900)
            return True

        # Generic modal with an X close button — only when the screen is
        # unrecognised (avoids false-positive taps on HOME elements).
        from cocbot.vision import ScreenType
        if self.vision.detect_screen(img) == ScreenType.UNKNOWN:
            btn = self.vision.find_button(img, "×")
            if btn is None:
                btn = self.vision.find_button(img, "X")
            # Real close buttons sit in the upper-right area of a modal
            if btn is not None and btn[0] > 500 and btn[1] < 300:
                log.info("Popup: generic modal – tapping X at %s", btn)
                self.device.tap(*btn, jitter=3)
                self._sleep(500, 900)
                return True

        return False

    def ensure_home(self) -> np.ndarray:
        """Go home, dismiss remaining popups, and return a home screenshot."""
        self.go_home()
        img = self.device.screenshot()
        # One more popup sweep
        if self.dismiss_popups(img):
            self._sleep(800, 1400)
            img = self.device.screenshot()
        return img

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sleep(self, min_ms: int, max_ms: int) -> None:
        """Sleep a random duration (humanization)."""
        ms = random.randint(min_ms, max_ms)
        time.sleep(ms / 1000.0)
