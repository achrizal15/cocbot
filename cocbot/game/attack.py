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
            # Tap tombol Serang! dengan jeda yang pasti
            self.device.tap(*btn_serang, jitter=2)
            self._sleep(800, 1200)

            # Cek jika ada pop-up konfirmasi (pasukan belum siap / pahlawan sedang pulih / mantra kurang)
            img_modal = self.device.screenshot()
            if self.vision.has_text(img_modal, ["belum", "siap", "yakin", "lengkap", "serang", "tidur", "pulih"]):
                log.info("[FARMING] Pop-up konfirmasi 'Pasukan/Pahlawan belum siap' terdeteksi -> Menekan Konfirmasi...")
                btn_ok = (
                    self.vision.find_button(img_modal, "oke", region=(500, 350, 900, 550))
                    or self.vision.find_button(img_modal, "serang", region=(500, 350, 900, 550))
                    or self.vision.find_button(img_modal, "ya", region=(500, 350, 900, 550))
                    or (775, 465)
                )
                self.device.tap(*btn_ok, jitter=2)
                self._sleep(1500, 2200)
            elif self.vision.detect_screen(img_modal) == ScreenType.ARMY_PREP:
                # Jika masih di ARMY_PREP (ketukan pertama belum tembus), tap sekali lagi
                self.device.tap(*btn_serang, jitter=2)
                self._sleep(1000, 1500)

            # Tunggu awan matchmaking CoC selesai (di TH15/16 pencarian musuh bisa butuh 30-60 detik)
            if not self._wait_for_search(65):
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

    def _wait_for_search(self, timeout: float = 65) -> bool:
        """Wait for SEARCH or BATTLE screen (has 'rampasan'). Tolerates cloud matchmaking up to 65s."""
        from cocbot.vision import ScreenType
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            img = self.device.screenshot()
            screen = self.vision.detect_screen(img)
            if screen in (ScreenType.SEARCH, ScreenType.BATTLE):
                return True
            if self.vision.has_text(img, ["rampasan", "tersedia", "berikut"]):
                return True
            # Dismiss pop-up modal jika muncul terlambat di tengah awan
            if self.vision.has_text(img, ["belum", "siap", "yakin", "lengkap"]):
                btn_ok = (
                    self.vision.find_button(img, "oke", region=(500, 350, 900, 550))
                    or self.vision.find_button(img, "serang", region=(500, 350, 900, 550))
                    or (775, 465)
                )
                self.device.tap(*btn_ok, jitter=2)
                self._sleep(1000, 1500)
            self._sleep(1000, 1500)
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

        total_slots = 10
        log.info("[SERANG - STEP 3] Memulai pengerahan universal (Zero-Failure Spread) untuk seluruh slot...")

        # Titik sebaran pantai kiri terbukti 100% bebas garis merah di base manapun
        spread_points = [
            (70, 260),   # Titik 1: Pantai atas
            (65, 320),   # Titik 2: Pantai tengah-atas
            (60, 380),   # Titik 3: Pantai tengah-bawah
            (70, 440),   # Titik 4: Pantai bawah
        ]
        spell_targets = [(640, 340), (590, 300), (690, 380), (640, 420)]

        def _get_cx(s: int) -> int:
            return int(145 + s * 100)

        # ── Pass 1: Kuras tuntas setiap kotak (Troops, Siege, Hero, Spell) ───
        for s_idx in range(total_slots):
            cx = _get_cx(s_idx)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.12)

            pt = spread_points[s_idx % len(spread_points)]
            alt_pt = spread_points[(s_idx + 1) % len(spread_points)]

            # 1. Tap 2x di rumput pantai (Seketika menurunkan Hero, Siege Machine, Golem, unit tunggal)
            self.device.tap(pt[0], pt[1], jitter=0)
            time.sleep(0.08)
            self.device.tap(alt_pt[0], alt_pt[1], jitter=0)
            time.sleep(0.08)

            # 2. Hold 2.0 detik (Menguras tuntas pasukan massal jika slot ini adalah troop)
            self.device.hold(pt[0], pt[1], duration_ms=2000)
            time.sleep(0.08)

            # 3. Lempar mantra beruntun (8x tap cepat ke base) agar stack mantra isi 4 s/d 8 habis tuntas (x0)
            for rep in range(8):
                spt = spell_targets[(s_idx + rep) % len(spell_targets)]
                self.device.tap(spt[0], spt[1], jitter=6)
                time.sleep(0.04)

        # ── Pass 2: Clean sweep (sapuan kedua untuk menguras sisa unit) ──────
        log.info("[SERANG - STEP 3] Melakukan sapuan kedua (clean sweep)...")
        time.sleep(0.3)
        for s_idx in range(total_slots):
            cx = _get_cx(s_idx)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.08)
            pt = spread_points[(s_idx + 2) % len(spread_points)]
            self.device.tap(pt[0], pt[1], jitter=0)
            time.sleep(0.06)
            self.device.hold(pt[0], pt[1], duration_ms=1500)
            time.sleep(0.06)
            for rep in range(4):
                spt = spell_targets[(s_idx + rep) % len(spell_targets)]
                self.device.tap(spt[0], spt[1], jitter=6)
                time.sleep(0.04)

        # ── Pass 3: Aktivasi kemampuan 4 Hero setelah 4 detik ───────────────
        log.info("[SERANG - STEP 3] Menunggu 4 detik lalu mengaktifkan kemampuan 4 Hero...")
        self._sleep(3800, 4800)
        for hs in range(2, 8):
            cx = _get_cx(hs)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.12)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.12)

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
                # Berikan jeda 6-8 detik agar CoC dan driver GPU menyelesaikan alokasi aset desa tanpa terganggu screencap
                self._sleep(6000, 8000)
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
        centers = [int(145 + i * 100) for i in range(10)]
        return 10, centers
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
