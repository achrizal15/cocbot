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

        preset = getattr(self.config.army, "preset", "2_troops")
        if preset == "3_troops":
            troop_slots = [0, 1, 2]
            siege_slots = [3]
            hero_slots = [4, 5, 6, 7]
            spell_slots = [8]
            log.info("[SERANG - STEP 3] Memulai deploy Formasi: 3 Troops, 1 Bengkel, 4 Hero, 1 Spell...")
        else:
            troop_slots = [0, 1]
            siege_slots = [2]
            hero_slots = [3, 4, 5, 6]
            spell_slots = [7]
            log.info("[SERANG - STEP 3] Memulai deploy Formasi: 2 Troops, 1 Bengkel, 4 Hero, 1 Spell...")

        strategy = getattr(self.config.army, "deploy", "all_slots")

        # Titik sebaran pantai kiri (tersebar rapi sepanjang garis pantai luar)
        left_shore = [
            (100, 230),  # Titik 1: Pantai atas
            (85, 290),   # Titik 2: Pantai tengah-atas
            (80, 350),   # Titik 3: Pantai tengah
            (85, 410),   # Titik 4: Pantai tengah-bawah
            (100, 470),  # Titik 5: Pantai bawah
        ]
        # Titik sebaran hutan kanan (untuk strategi sebar 2 sisi)
        right_forest = [
            (1170, 230), # Titik 1: Hutan atas
            (1190, 290), # Titik 2: Hutan tengah-atas
            (1200, 350), # Titik 3: Hutan tengah
            (1190, 410), # Titik 4: Hutan tengah-bawah
            (1170, 470), # Titik 5: Hutan bawah
        ]

        spell_targets = [(640, 340), (580, 300), (700, 380), (640, 420)]

        def _get_cx(s: int) -> int:
            return int(127 + s * 100)

        # ── 1. Turunkan Bengkel / Siege Machine di Sudut Aman ───────────────
        for s in siege_slots:
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.15)
            # Turunkan di sudut pantai atas agar menembak aman dari jarak jauh
            bengkel_pt = left_shore[0]
            self.device.tap(bengkel_pt[0], bengkel_pt[1], jitter=0)
            time.sleep(0.08)
            self.device.tap(bengkel_pt[0], bengkel_pt[1], jitter=0)
            time.sleep(0.12)

        # ── 2. Turunkan Troops Massal Menyebar di Sepanjang Garis Pantai ─────
        log.info("[SERANG - STEP 3] Menyebarkan pasukan massal di sepanjang garis pantai...")
        for idx, s in enumerate(troop_slots):
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.15)

            if strategy == "spread_sides" and idx % 2 == 1:
                # Sebar di sisi hutan kanan
                pts = [right_forest[0], right_forest[2], right_forest[4]]
            else:
                # Sebar di sisi pantai kiri
                pts = [left_shore[0], left_shore[2], left_shore[4]] if idx == 0 else [left_shore[1], left_shore[3]]

            # Hold terbagi di beberapa titik berbeda agar pasukan menyebar menjadi barisan
            hold_time = int(1800 / len(pts))
            for p in pts:
                self.device.hold(p[0], p[1], duration_ms=hold_time)
                time.sleep(0.06)
                self.device.tap(p[0], p[1], jitter=0)
                time.sleep(0.06)

        # ── 3. Turunkan 4 Hero Menyebar Rapi Bersama Pasukan ────────────────
        log.info("[SERANG - STEP 3] Menurunkan 4 Hero menyebar rapi...")
        hero_drop_points = [
            left_shore[1],  # King di pantai tengah-atas
            left_shore[2],  # Queen di pantai tengah
            left_shore[3],  # Warden di pantai tengah-bawah
            left_shore[4],  # Champion di pantai bawah
        ]
        if strategy == "spread_sides" and len(hero_slots) >= 4:
            # 2 Hero di kiri (King & Queen), 2 Hero di kanan (Warden & Champion)
            hero_drop_points = [left_shore[1], left_shore[3], right_forest[1], right_forest[3]]

        for h_idx, s in enumerate(hero_slots):
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.15)
            h_pt = hero_drop_points[h_idx % len(hero_drop_points)]
            self.device.tap(h_pt[0], h_pt[1], jitter=0)
            time.sleep(0.08)
            self.device.tap(h_pt[0], h_pt[1], jitter=0)
            time.sleep(0.12)

        # ── 4. Lempar Spell Menyebar di Core Base Musuh ────────────────────
        for s in spell_slots:
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.15)
            for spt in spell_targets + spell_targets[:2]:
                self.device.tap(spt[0], spt[1], jitter=4)
                time.sleep(0.08)

        # ── 5. Clean Sweep (Sapuan ulang cepat menyebar) ───────────────────
        time.sleep(0.3)
        for idx, s in enumerate(troop_slots):
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.08)
            p = left_shore[(idx + 2) % len(left_shore)]
            self.device.hold(p[0], p[1], duration_ms=1000)
            time.sleep(0.06)

        for h_idx, s in enumerate(hero_slots):
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.08)
            h_pt = hero_drop_points[h_idx % len(hero_drop_points)]
            self.device.tap(h_pt[0], h_pt[1], jitter=0)
            time.sleep(0.06)

        for s in spell_slots:
            self.device.tap(_get_cx(s), 665, jitter=0)
            time.sleep(0.08)
            for spt in spell_targets[:3]:
                self.device.tap(spt[0], spt[1], jitter=4)
                time.sleep(0.06)

        # ── 6. Aktivasi Kemampuan Spesial 4 Hero setelah 4 detik ────────────
        log.info("[SERANG - STEP 3] Menunggu 4 detik lalu mengaktifkan kemampuan 4 Hero...")
        self._sleep(3800, 4800)
        for s in hero_slots:
            cx = _get_cx(s)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.15)
            self.device.tap(cx, 665, jitter=0)
            time.sleep(0.15)

        log.info("[SERANG - STEP 3 SELESAI] Seluruh pasukan, bengkel, 4 hero, dan spell telah menyebar tuntas!")
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
        centers = [int(127 + i * 100) for i in range(10)]
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
