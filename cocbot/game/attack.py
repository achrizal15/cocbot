"""Attack search loop and troop deployment."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from cocbot.adb import AdbDevice
    from cocbot.config import Profile
    from cocbot.vision import Loot, Vision

log = logging.getLogger("cocbot")

# Layout (1280x720) — coordinates verified with live OCR
_BTN_ATTACK     = (85, 650)    # Attack sword icon on home (center of button)
_BTN_FIND_MATCH = (280, 525)   # "Cari Lawan Tanding" on attack menu (center of button)
_BTN_SERANG     = (1150, 640)  # "Serang!" on army prep (bottom-right)
_BTN_NEXT       = (1189, 497)  # "Berikutnya" during search (verified OCR)
_BTN_END_BATTLE = (70, 530)    # "Akhiri Serangan" during battle
# Diamond corners for troop deployment lines (1280×720).
# Stay inside the valid green zone:  TOP below HUD,  BOTTOM above troop bar.
_TOP    = (640, 100)
_BOTTOM = (640, 570)
_LEFT   = (60, 330)
_RIGHT  = (1220, 330)

# Side edges: each side is a line between two diamond corners
_SIDES = [
    (_LEFT, _TOP),        # 0: top-left edge
    (_TOP, _RIGHT),       # 1: top-right edge
    (_BOTTOM, _LEFT),     # 2: bottom-left edge
    (_RIGHT, _BOTTOM),    # 3: bottom-right edge
]

_DEPLOY_POINTS_PER_SIDE = 15

# Troop bar region (1280×720) — measured from game screenshot:
# Card 0: X=101..193 (center 147), Card 1: X=199..290 (center 245), etc.
# Slot pitch ≈ 97px.
_TROOP_BAR_START_X = 101 # left edge of first card
_TROOP_SLOT_W      = 97  # slot pitch (card width ~91 + gap ~6)
_TROOP_BAR_Y       = 660 # centre Y of troop cards
_MAX_TROOP_SLOTS   = 10
_TROOP_SLOT_STD    = 35  # std threshold: cards have std > 50, empty background < 33

def _lerp_points(p1: tuple[int, int], p2: tuple[int, int], n: int) -> list[tuple[int, int]]:
    """Generate *n* evenly spaced points along the line from *p1* to *p2*."""
    pts: list[tuple[int, int]] = []
    for i in range(n):
        t = i / max(n - 1, 1)
        x = int(p1[0] + (p2[0] - p1[0]) * t)
        y = int(p1[1] + (p2[1] - p1[1]) * t)
        pts.append((x, y))
    return pts


class Attacker:
    """Handles search-loop and troop deployment."""

    def __init__(self, device: AdbDevice, vision: Vision, config: Profile) -> None:
        self.device = device
        self.vision = vision
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_attack(self) -> bool:
        """Core Farming:
        Step 1: Tekan tombol pertempuran (Pedang di kiri bawah 85, 650).
        Step 2: Pastikan floating menu pertempuran terbuka dan ada tombol 'Cari Lawan Tanding'.
                Jika TIDAK ADA -> tutup / reset ke Beranda dan ulangi dari awal.
                Jika ADA -> tekan 'Cari Lawan Tanding' -> 'Serang!' -> masuk pencarian desa.
        """
        from cocbot.vision import ScreenType

        # Pastikan tidak ada bangunan/modal terseleksi di desa
        self.device.tap(200, 50, jitter=3)
        self._sleep(300, 500)

        # ── Step 1: Tekan tombol Pertempuran ───────────────────────────────
        log.info("[FARMING - STEP 1] Menekan tombol Pertempuran (Pedang) di kiri bawah...")
        self.device.tap(*_BTN_ATTACK, jitter=4)
        self._sleep(1200, 1800)

        # ── Step 2: Verifikasi floating menu & tombol 'Cari Lawan Tanding' ──
        log.info("[FARMING - STEP 2] Memeriksa floating menu & tombol 'Cari Lawan Tanding'...")
        img_menu = self.device.screenshot()

        # Deteksi tombol 'Cari Lawan Tanding' via OCR kata 'tanding' / 'lawan' / 'cari'
        btn_find = (
            self.vision.find_button(img_menu, "tanding", region=(50, 460, 450, 580))
            or self.vision.find_button(img_menu, "lawan", region=(50, 460, 450, 580))
            or self.vision.find_button(img_menu, "cari", region=(50, 460, 450, 580))
        )

        # Fallback warna oranye khas tombol Cari Lawan Tanding di (280, 525)
        if btn_find is None and img_menu is not None:
            px = img_menu[520, 280]
            if int(px[2]) > 180 and int(px[1]) > 120 and int(px[0]) < 100:
                btn_find = _BTN_FIND_MATCH

        if btn_find is None:
            log.warning(
                "[FARMING - STEP 2 GAGAL] Floating menu pertempuran TIDAK terbuka atau tombol 'Cari Lawan Tanding' TIDAK ada! "
                "Mereset ke Beranda dan mengulangi dari awal..."
            )
            self.device.tap(1000, 300, jitter=4)
            self._sleep(500, 800)
            self.device.tap(200, 50, jitter=3)
            self._sleep(500, 800)
            return False

        log.info("[FARMING - STEP 2 SUKSES] Tombol 'Cari Lawan Tanding' terverifikasi di %s -> Menekan tombol...", btn_find)
        self.device.tap(*btn_find, jitter=4)
        self._sleep(1500, 2200)

        # Tunggu layar Persiapan Pasukan (Tentaraku) atau langsung matchmaking
        screen_reached: ScreenType | None = None
        for _ in range(10):
            img_chk = self.device.screenshot()
            s = self.vision.detect_screen(img_chk)
            if s in (ScreenType.SEARCH, ScreenType.BATTLE) and self.vision.has_text(img_chk, ["rampasan", "tersedia", "berikut"]):
                screen_reached = ScreenType.SEARCH
                break
            if s == ScreenType.ARMY_PREP or self.vision.has_text(img_chk, ["tentaraku", "serang", "resep"]):
                screen_reached = ScreenType.ARMY_PREP
                break
            self._sleep(600, 1000)

        if screen_reached == ScreenType.ARMY_PREP:
            log.info("[FARMING] Layar Persiapan Pasukan (Tentaraku) terbuka -> Menekan tombol 'Serang!'...")
            img_prep = self.device.screenshot()
            btn_serang = (
                self.vision.find_button(img_prep, "serang", region=(900, 580, 1280, 720))
                or _BTN_SERANG
            )
            self.device.tap(*btn_serang, jitter=4)
            self._sleep(1000, 1500)

            # Cek pop-up konfirmasi pasukan belum siap jika muncul
            img_modal = self.device.screenshot()
            if self.vision.has_text(img_modal, ["belum", "siap", "yakin", "lengkap"]):
                log.info("[FARMING] Pop-up 'Pasukan belum lengkap' terdeteksi -> Menekan Konfirmasi...")
                btn_ok = (
                    self.vision.find_button(img_modal, "oke")
                    or self.vision.find_button(img_modal, "serang")
                    or self.vision.find_button(img_modal, "ya")
                    or (775, 465)
                )
                self.device.tap(*btn_ok, jitter=3)
                self._sleep(1500, 2500)

            if not self._wait_for_search(30):
                log.warning("[FARMING] Matchmaking berlangsung lama / belum masuk ke desa lawan")
                return False
        elif screen_reached == ScreenType.SEARCH:
            log.info("[FARMING] Langsung masuk ke layar pencarian lawan!")
        else:
            log.warning("[FARMING] Tidak berhasil menuju persiapan pasukan atau pencarian lawan — mengulang dari awal")
            self.device.tap(1000, 300)
            self._sleep(500, 800)
            return False

        log.info("[FARMING] Berhasil masuk ke pertempuran desa lawan!")
        return True
    def _wait_for_search(self, timeout: float) -> bool:
        """Wait for SEARCH or BATTLE screen (has 'rampasan')."""
        from cocbot.vision import ScreenType
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            img = self.device.screenshot()
            screen = self.vision.detect_screen(img)
            if screen in (ScreenType.SEARCH, ScreenType.BATTLE):
                return True
            if self.vision.has_text(img, ["rampasan", "berikut"]):
                return True
            self._sleep(800, 1200)
        return False

    def search_target(self) -> Loot | None:
        """Search for a base that meets loot thresholds.

        Returns the *Loot* of the chosen target, or ``None`` if *max_next* is
        exceeded without finding a match.
        """
        cfg = self.config.search
        max_next = getattr(cfg, "max_next", 60)
        relax_after = min(getattr(cfg, "relax_after_next", 10), max(1, max_next // 3))
        relax_pct = 0.85
        min_g = getattr(cfg, "min_gold", 1)
        min_e = getattr(cfg, "min_elixir", 1)
        min_d = getattr(cfg, "min_dark", 1)
        rule = getattr(cfg, "rule", "any")
        log.info(
            "[CARI TARGET] Memulai evaluasi desa lawan (Target Min: Gold %s, Elixir %s, Dark %s | Aturan: %s | Maks Next: %d)",
            f"{min_g:,}", f"{min_e:,}", f"{min_d:,}", rule, max_next,
        )

        for i in range(max_next):
            img = self.device.screenshot()
            loot = self.vision.read_loot(img)

            if loot is None or (loot.gold == 0 and loot.elixir == 0):
                log.debug("[CARI TARGET] Deteksi loot desa #%d sedang membaca...", i + 1)
                self._sleep(800, 1200)
                continue

            # Relaxation: decrease thresholds after relax_after
            relaxation = 1.0
            if i >= relax_after and max_next > relax_after:
                progress = (i - relax_after) / (max_next - relax_after)
                relaxation = max(0.1, 1.0 - relax_pct * progress)

            ok, reason = self._check_loot_detailed(loot, relaxation)
            if ok:
                log.info(
                    "[CARI TARGET #%d] TARGET DITERIMA! G=%s | E=%s | D=%s (Relaksasi: %.0f%% | Alasan: %s) -> Menyerang!",
                    i + 1, f"{loot.gold:,}", f"{loot.elixir:,}", f"{loot.dark:,}",
                    relaxation * 100, reason,
                )
                return loot

            log.info(
                "[CARI TARGET #%d] Dilewati: G=%s | E=%s | D=%s (Alasan: %s) -> Menekan Berikutnya...",
                i + 1, f"{loot.gold:,}", f"{loot.elixir:,}", f"{loot.dark:,}", reason,
            )
            self.device.tap(*_BTN_NEXT, jitter=4)
            self._sleep(1800, 2600)
        log.info("[CARI TARGET] Mencapai batas pencarian (%d desa) -> Langsung menyerang desa saat ini!", max_next)
        img = self.device.screenshot()
        return self.vision.read_loot(img) or Loot(gold=1, elixir=1, dark=0)


    def deploy_troops(self) -> bool:
        """Core Farming Step 3:
        Iterasi per kotak bilah pasukan dari kiri ke kanan (0..max_slots)
        dan kerahkan SEMUA troops, heroes, siege, dan spells sampai 100% kosong.
        """
        img = self.device.screenshot()
        if img is None:
            return False

        # Titik perimeter kiri (sungai/pantai) yang 100% di luar red line pada base manapun
        safe_left = [
            (50, 240), (50, 280), (50, 320), (50, 360), (50, 400), (50, 440)
        ]
        # Titik perimeter atas (rumput atas di luar base)
        safe_top = [
            (560, 70), (640, 70), (720, 70)
        ]
        # Titik core desa untuk melempar spell
        core_targets = [
            (640, 340), (600, 300), (680, 380), (640, 420)
        ]

        total_slots, centers = self._detect_tray_slots(img)
        log.info("[SERANG - STEP 3] Memulai deploy presisi (multi-pass clean deploy) untuk %d slot...", total_slots)

        # Titik perimeter rumput valid di luar garis merah (bukan di air/tebing, bukan di top banner)
        drop_points = [(140, 260), (120, 320), (140, 380), (350, 140)]
        spell_targets = [(640, 360), (580, 320), (700, 380), (640, 420)]

        # Tentukan perkiraan batas slot spell (biasanya 4-5 slot terakhir adalah spell)
        spell_start_idx = max(total_slots - 5, 7)

        # ── Pass 1: Kerahkan seluruh unit, hero, siege, dan spell ───────────
        for s_idx in range(total_slots):
            cx = centers[s_idx]
            # 1. Pilih kotak kartu pasukan (tepat di tengah kotak, zero jitter)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.18)
            pt = drop_points[s_idx % len(drop_points)]

            if s_idx < spell_start_idx:
                # Hold 1.8 detik di titik aman utama untuk menguras pasukan massal
                self.device.hold(pt[0], pt[1], duration_ms=1800)
                time.sleep(0.10)
                # Tap di 2 titik berbeda di sepanjang pantai aman untuk menjamin Hero / Siege / Golem langsung turun
                alt_pt = drop_points[(s_idx + 1) % len(drop_points)]
                self.device.tap(pt[0], pt[1], jitter=0)
                time.sleep(0.08)
                self.device.tap(alt_pt[0], alt_pt[1], jitter=0)
                time.sleep(0.08)
            else:
                # Khusus slot spell: tap berkali-kali ke core base untuk melempar seluruh stack (misal Earthquake x4)
                for spt in spell_targets + spell_targets[:2]:
                    self.device.tap(spt[0], spt[1], jitter=6)
                    time.sleep(0.10)

            time.sleep(0.15)

        # ── Pass 2: Clean sweep (sapuan ulang untuk menguras tuntas sisa pasukan) ─
        log.info("[SERANG - STEP 3] Melakukan sapuan kedua (clean sweep) agar tidak ada sisa pasukan...")
        time.sleep(0.4)
        for s_idx in range(total_slots):
            cx = centers[s_idx]
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.14)
            pt = drop_points[(s_idx + 1) % len(drop_points)]
            if s_idx < spell_start_idx:
                alt_pt = drop_points[(s_idx + 2) % len(drop_points)]
                time.sleep(0.06)
                self.device.tap(alt_pt[0], alt_pt[1], jitter=0)
                time.sleep(0.06)
                self.device.hold(pt[0], pt[1], duration_ms=1200)
                time.sleep(0.08)
            else:
                for spt in spell_targets:
                    self.device.tap(spt[0], spt[1], jitter=6)
                    time.sleep(0.08)

        # ── Pass 3: Aktivasi kemampuan Hero setelah 4.5 detik ────────────────
        log.info("[SERANG - STEP 3] Menunggu 4.5 detik lalu mengaktifkan kemampuan spesial Hero...")
        self._sleep(4000, 5000)
        # Hero biasanya berada di rentang slot 3 sampai 7
        for hs in range(3, min(8, total_slots)):
            cx = centers[hs]
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.15)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.15)

        log.info("[SERANG - STEP 3 SELESAI] Seluruh pasukan, hero, siege, dan spell telah 100%% dikerahkan!")
        return True

    def end_battle(self) -> None:
        """End the current battle / search session without deploying."""
        from cocbot.vision import ScreenType

        log.info("Ending battle / search session")
        self.device.tap(*_BTN_END_BATTLE, jitter=5)
        self._sleep(1200, 1800)
        img = self.device.screenshot()
        if img is None or self.vision.detect_screen(img) == ScreenType.HOME:
            return
        # Confirm surrender dialog only if present
        btn = self.vision.find_button(img, "menyerah") or self.vision.find_button(img, "oke") or self.vision.find_button(img, "ya")
        if btn:
            self.device.tap(*btn, jitter=4)
            self._sleep(2000, 3000)

    def wait_battle_end(self) -> bool:
        """Wait for battle to finish (max 3 minutes)."""
        from cocbot.vision import ScreenType

        deadline = time.monotonic() + 180  # 3 minutes
        while time.monotonic() < deadline:
            img = self.device.screenshot()
            screen = self.vision.detect_screen(img)

            # Battle ended — we're back at home or a results screen
            if screen == ScreenType.HOME:
                log.info("Battle ended – home screen detected")
                return True

            # Results screen: "Ke Beranda" = return home
            if self.vision.has_text(img, ["beranda", "kembali", "pulang"]):
                log.info("Battle results – tapping Return Home")
                btn = self.vision.find_button(img, "beranda")
                if btn is None:
                    btn = self.vision.find_button(img, "kembali")
                if btn:
                    self.device.tap(*btn, jitter=5)
                else:
                    self.device.tap(640, 600, jitter=5)
                # Berikan jeda 4-5 detik agar CoC dan GPU emulator menyelesaikan alokasi aset desa
                self._sleep(4000, 5500)
                return True
            self._sleep(4000, 6000)

        log.warning("Battle timeout (3 min) – force ending")
        # Try tapping end battle then surrender
        self.device.tap(*_BTN_END_BATTLE, jitter=5)
        self._sleep(1000, 2000)
        # Confirm surrender
        btn_pos = self.vision.find_button(self.device.screenshot(), "menyerah")
        if btn_pos:
            self.device.tap(*btn_pos, jitter=4)
        else:
            self.device.tap(640, 400, jitter=5)
        self._sleep(2000, 3000)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _check_loot_detailed(self, loot: Loot, relaxation: float = 1.0) -> tuple[bool, str]:
        """Evaluate loot against search thresholds with explicit reason message."""
        cfg = self.config.search
        rule = getattr(cfg, "rule", "any")

        min_gold = int(getattr(cfg, "min_gold", 0) * relaxation)
        min_elixir = int(getattr(cfg, "min_elixir", 0) * relaxation)
        min_dark = int(getattr(cfg, "min_dark", 0) * relaxation)

        gold_val = loot.gold or 0
        elixir_val = loot.elixir or 0
        dark_val = loot.dark or 0

        gold_ok = gold_val >= min_gold
        elixir_ok = elixir_val >= min_elixir
        dark_ok = dark_val >= min_dark

        if rule == "gold_and_elixir":
            if gold_ok and elixir_ok:
                return True, f"Gold ({gold_val:,} >= {min_gold:,}) & Elixir ({elixir_val:,} >= {min_elixir:,}) keduanya lolos"
            reasons = []
            if not gold_ok:
                reasons.append(f"Gold kurang ({gold_val:,} < {min_gold:,})")
            if not elixir_ok:
                reasons.append(f"Elixir kurang ({elixir_val:,} < {min_elixir:,})")
            return False, f"Aturan 'Wajib Keduanya' gagal: {', '.join(reasons)}"

        elif rule in ("any", "gold_or_elixir"):
            # Pertimbangkan target yang aktif (> 10)
            checks = []
            if min_gold > 10:
                checks.append(("Gold", gold_ok, gold_val, min_gold))
            if min_elixir > 10:
                checks.append(("Elixir", elixir_ok, elixir_val, min_elixir))
            if min_dark > 10:
                checks.append(("Dark", dark_ok, dark_val, min_dark))

            if not checks:
                # Jika semua default 1, loloskan jika ada sumber daya apa pun
                return (gold_val > 0 or elixir_val > 0), "Target minimum default terpenuhi"

            matched = [f"{name} ({val:,} >= {req:,})" for name, ok, val, req in checks if ok]
            if matched:
                return True, f"Aturan 'Salah Satu Memenuhi': {', '.join(matched)}"

            unmatched = [f"{name} kurang ({val:,} < {req:,})" for name, ok, val, req in checks if not ok]
            return False, f"Aturan 'Salah Satu Memenuhi' gagal: {', '.join(unmatched)}"

        elif rule == "all":
            if gold_ok and elixir_ok and dark_ok:
                return True, "Seluruh sumber daya (Gold, Elixir, Dark) memenuhi target"
            fails = []
            if not gold_ok:
                fails.append(f"Gold ({gold_val:,} < {min_gold:,})")
            if not elixir_ok:
                fails.append(f"Elixir ({elixir_val:,} < {min_elixir:,})")
            if not dark_ok:
                fails.append(f"Dark ({dark_val:,} < {min_dark:,})")
            return False, f"Aturan 'Semua Wajib Memenuhi' gagal: {', '.join(fails)}"

        return (gold_ok and elixir_ok), "Default rule evaluated"

    def _check_loot(self, loot: Loot, relaxation: float = 1.0) -> bool:
        ok, _ = self._check_loot_detailed(loot, relaxation)
        return ok

    def _wait_screen(self, target: ScreenType, timeout: float) -> bool:
        """Poll for a target screen."""
        while time.monotonic() < deadline:
            img = self.device.screenshot()
            if self.vision.detect_screen(img) == target:
                return True
            self._sleep(1000, 1800)
    def _detect_tray_slots(self, img: np.ndarray | None = None) -> tuple[int, list[int]]:
        """Detect exact number of cards (9..14) in the battle tray and compute exact centers."""
        import cv2
        if img is None:
            img = self.device.screenshot()
        if img is None or getattr(img, "size", 0) == 0:
            w = 1280.0 / 12.0
            return 12, [int((i + 0.5) * w) for i in range(12)]

        tray = img[640:710, :]
        gray = cv2.cvtColor(tray, cv2.COLOR_BGR2GRAY)

        best_n = 12
        best_score = -1e9
        for n in range(9, 15):
            w = 1280.0 / n
            border_scores = []
            for k in range(1, n):
                bx = int(k * w)
                left_center = int((k - 0.5) * w)
                border_val = float(gray[:, max(0, bx - 3):min(1280, bx + 3)].mean())
                center_val = float(gray[:, max(0, left_center - 10):min(1280, left_center + 10)].mean())
                border_scores.append(center_val - border_val)
            score = float(np.mean(border_scores))
            if score > best_score:
                best_score = score
                best_n = n

        w = 1280.0 / best_n
        centers = [int((i + 0.5) * w) for i in range(best_n)]
        return best_n, centers

    def _get_slot_center(self, slot_idx: int, img: np.ndarray | None = None) -> tuple[int, int]:
        """Compute screen (x, y) center coordinates for a slot."""
        _, centers = self._detect_tray_slots(img)
        if slot_idx < len(centers):
            return (centers[slot_idx], 665)
        cx = int((slot_idx + 0.5) * (1280.0 / max(len(centers), 12)))
        return (cx, 665)

    def _detect_troop_slots(self, img: np.ndarray | None = None) -> list[int]:
        """Return a list of valid slot indices in the troop bar."""
        n, _ = self._detect_tray_slots(img)
        return list(range(n))

    def _tap_troop_slot(self, slot_index: int, img: np.ndarray | None = None) -> None:
        """Tap a troop slot card in the troop bar by index (zero jitter to avoid neighbor collision)."""
        cx, cy = self._get_slot_center(slot_index, img)
        self.device.tap(cx, cy, jitter=0)

    def _is_slot_empty(self, slot_index: int, img: np.ndarray | None = None) -> bool:
        """Check if a troop slot is empty/grayed-out (0 troops remaining)."""
        import cv2
        if img is None:
            img = self.device.screenshot()
        if img is None:
            return True

        start_x, card_w, _ = self._get_slot_geometry(img)
        x1 = int(start_x + slot_index * card_w)
        x2 = int(start_x + (slot_index + 1) * card_w)
        crop = img[630:700, x1+10:x2-10]
        if crop.size == 0:
            return True
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        sat = float(hsv[:, :, 1].mean())
        val = float(hsv[:, :, 2].mean())
        # Empty / deployed slots are grayed out (sat < 45 and val < 95)
        return (sat < 45.0 and val < 95.0)
    def _sleep(self, min_ms: int, max_ms: int) -> None:
        ms = random.randint(min_ms, max_ms)
        time.sleep(ms / 1000.0)
