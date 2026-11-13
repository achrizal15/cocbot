"""Builder upgrade logic — read suggestions, pick best, confirm."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from cocbot.adb import AdbDevice
    from cocbot.config import Profile
    from cocbot.vision import Resources, Vision

from cocbot.vision import (
    ScreenType,
    _detect_screen_fast,
    _fuzzy_match,
    _has_color_in_region,
    _normalize,
    find_panel_entries,
)

log = logging.getLogger("cocbot")

# Layout (1280x720) — center of 'Bangun' button at bottom-right
_BTN_BUILD = (1195, 650)

# Build-suggestion panel lives on the right side of the screen (below header)
_BUILD_MENU_REGION = (940, 70, 1280, 645)
# Priority mapping from config keywords to category keywords in OCR text
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "storage":   ["penyimpanan", "storage", "gudang", "bank"],
    "collector": ["penambang", "collector", "pompa", "tambang", "mine", "pump", "pengumpul", "eliksir"],
    "army":      ["barak", "barrack", "laboratorium", "lab", "pabrik", "factory", "camp", "kamp"],
    "defense":   ["pertahanan", "menara", "meriam", "tower", "cannon", "mortar", "mortir", "inferno", "eagle",
                  "pemanah", "archer", "wizard", "penyihir", "tesla", "bom", "bomb", "air", "udara"],
    "hero":      ["pahlawan", "hero", "raja", "ratu", "king", "queen", "warden", "champion", "royal"],
    "wall":      ["tembok", "tepabok", "teeabok", "tepbok", "temb0k", "dinding", "wall"],
}

class Builder:
    """Check for free builders and start upgrades."""

    def __init__(self, device: AdbDevice, vision: Vision, config: Profile) -> None:
        self.device = device
        self.vision = vision
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_and_upgrade(self, resources: Resources) -> bool:
        """If a builder is free, pick and confirm an upgrade (or walls).

        Returns True if an upgrade was started.
        """
        if not getattr(self.config.upgrade, "enabled", True):
            log.debug("Upgrades disabled in config")
            return False

        cfg_up = self.config.upgrade
        sort_by = getattr(cfg_up, "sort_by", "priority")
        priority = getattr(cfg_up, "priority", [])
        is_wall_only = (sort_by == "wall" or (priority and priority[0] == "wall"))

        # 1. If user set mode to WALL ONLY: STRICTLY upgrade walls only!
        if is_wall_only:
            log.info("Upgrade mode: WALL ONLY — checking walls across base")
            walls_done = self.upgrade_walls(resources)
            if walls_done > 0:
                log.info("Wall upgrade completed (%d walls upgraded)", walls_done)
                return True
            log.info("Wall upgrade focus: no eligible walls to upgrade — skipping building upgrades")
            return False

        # 2. Check builder availability
        img = self.device.screenshot()
        builder_status = self.vision.read_builder_status(img)
        free = getattr(builder_status, "free", 0)
        if free <= 0:
            log.debug("No free builders (free=%s)", free)
            return False
        log.info("Free builders: %d", free)

        # 3. Open build suggestions by tapping the builder icon
        self.device.tap(*_BTN_BUILD, jitter=4)

        img = None
        for _ in range(8):
            self._sleep(250, 400)
            img = self.device.screenshot()
            if self.vision.detect_screen(img) == ScreenType.BUILD_SUGGEST:
                break
        else:
            log.info("Build suggestion panel did not open — recovering")
            self._dismiss_panel()
            return False

        # Scan visible suggestions; if none match mode, scroll down up to 2 times
        for scroll_idx in range(3):
            suggestions = self._read_build_menu(img)
            pick = self._pick_upgrade(suggestions, resources) if suggestions else None
            if pick is not None:
                log.info(
                    "Starting upgrade: %s (cost: %s, category: %s)",
                    pick.get("name"),
                    pick.get("cost"),
                    pick.get("category"),
                )
                ok = self._confirm_upgrade(pick)
                if not ok:
                    self._dismiss_panel()
                return ok

            if scroll_idx < 2:
                self.device.swipe(1100, 560, 1100, 160, duration_ms=300)
                self._sleep(350, 500)
                img = self.device.screenshot()

        log.info("No upgrade matching mode '%s' and available resources", sort_by)
        self._dismiss_panel()
        return False

    # Verified outer-perimeter wall coordinates on 1280x720 village map
    _WALL_SEEDS = [
        (840, 440),
        (420, 350),
        (600, 480),
        (660, 460),
        (720, 420),
        (500, 320),
        (560, 360),
        (620, 380),
        (680, 360),
        (740, 320),
        (640, 220),
        (580, 250),
        (700, 250),
        (520, 280),
        (760, 280),
        (350, 380),
        (780, 480),
    ]

    @staticmethod
    def _find_action_cards(img: object) -> list[tuple[int, int, int]]:
        """Dynamically detect bottom action-bar cards regardless of panel state or TH level.

        In COC (1280x720), bottom action-bar cards have a 4px dark shadow border at Y=623..627
        and a bright cream interior at Y=535..580.
        Returns a list of ``(x1, x2, center_x)`` sorted left-to-right.
        """

        arr = img
        if arr is None or getattr(arr, "size", 0) == 0:
            return []
        sub = arr[623:627, 150:920]
        bot_dark = sub.mean(axis=2).min(axis=0) < 32
        cards: list[tuple[int, int, int]] = []
        start: int | None = None
        for x, val in enumerate(bot_dark):
            if val and start is None:
                start = x
            elif not val and start is not None:
                w = x - start
                if 85 <= w <= 135:
                    cards.append((150 + start, 150 + x, 150 + (start + x) // 2))
                start = None
        if start is not None:
            w = len(bot_dark) - start
            if 85 <= w <= 135:
                cards.append((150 + start, 150 + len(bot_dark), 150 + (start + len(bot_dark)) // 2))
        return cards

    def upgrade_walls(self, resources: Resources, max_upgrades: int = 50) -> int:
        """Direct Map Wall Upgrade — ZERO panel, ZERO scroll, 100% template matching.

        Upgrades walls directly on the village map:
        1. Deselect any active modal/building.
        2. Tap verified wall seeds on the village map (takes 0.3s).
        3. Match "btn_pilih_baris", "btn_upgrade_gold", or "btn_upgrade_elixir" (<2ms).
        4. Select Gold or Elixir upgrade button based on higher balance.
        5. Tap the upgrade button -> confirm modal opens.
        6. Match "btn_confirm_modal" template (<2ms) and tap Confirm!
        7. Repeats until Gold/Elixir depleted or max_upgrades reached.
        """
        cfg_up = self.config.upgrade
        kr = getattr(cfg_up, "keep_reserve", {})
        keep_gold = kr.get("gold", 15000) if isinstance(kr, dict) else getattr(kr, "gold", 15000)
        keep_elixir = kr.get("elixir", 15000) if isinstance(kr, dict) else getattr(kr, "elixir", 15000)
        cur_gold = getattr(resources, "gold", 0) or 0
        cur_elixir = getattr(resources, "elixir", 0) or 0

        avail_gold = max(0, cur_gold - keep_gold)
        avail_elixir = max(0, cur_elixir - keep_elixir)
        min_upgrade_threshold = 100_000
        if max(avail_gold, avail_elixir) < min_upgrade_threshold:
            log.info("[UPGRADE WALL] Saldo Gold (%s) dan Elixir (%s) habis/kurang — langsung beralih ke farming!", f"{avail_gold:,}", f"{avail_elixir:,}")
            return 0

        upgraded = 0
        t_start = time.time()
        seed_idx = 0

        for round_idx in range(max_upgrades):
            if max(avail_gold, avail_elixir) < min_upgrade_threshold:
                log.info("[UPGRADE WALL] Saldo Gold (%s) dan Elixir (%s) telah habis — beralih ke farming!", f"{avail_gold:,}", f"{avail_elixir:,}")
                break
            # Iterate through verified wall seeds on the map until an UNMAXED wall is found
            wall_upgraded_this_round = False

            for attempt in range(len(self._WALL_SEEDS)):
                seed = self._WALL_SEEDS[(seed_idx + attempt) % len(self._WALL_SEEDS)]
                self.device.tap(200, 50)  # clean deselect
                self._sleep(100, 180)
                self.device.tap(seed[0], seed[1])
                self._sleep(300, 450)

                img_sel = self.device.screenshot()
                if img_sel is None:
                    continue

                # Fast Template Match (<2ms)
                pt_pb = self.vision.find_template(img_sel, "btn_pilih_baris", threshold=0.75, region=(350, 480, 600, 660))
                pt_ug = self.vision.find_template(img_sel, "btn_upgrade_gold", threshold=0.70, region=(650, 480, 980, 660))
                pt_ue = self.vision.find_template(img_sel, "btn_upgrade_elixir", threshold=0.70, region=(650, 480, 980, 660))

                if not (pt_pb or pt_ug or pt_ue):
                    # Not a wall at this seed
                    continue

                if pt_ug is None and pt_ue is None:
                    # Wall is already maxed (no upgrade buttons on action bar)
                    log.info("[UPGRADE WALL] Tembok di seed %s sudah level MAX (tanpa tombol upgrade) — mencari tembok lain...", seed)
                    continue
                # Check which resource has enough balance to upgrade
                # NEVER select a resource whose balance is exhausted!
                can_pay_gold = (avail_gold >= min_upgrade_threshold and pt_ug is not None)
                can_pay_elixir = (avail_elixir >= min_upgrade_threshold and pt_ue is not None)

                if can_pay_gold and can_pay_elixir:
                    # Both available: choose whichever resource has higher balance
                    if avail_gold >= avail_elixir:
                        target_pt = pt_ug
                        res_type = "GOLD"
                    else:
                        target_pt = pt_ue
                        res_type = "ELIXIR"
                elif can_pay_gold:
                    target_pt = pt_ug
                    res_type = "GOLD"
                elif can_pay_elixir:
                    target_pt = pt_ue
                    res_type = "ELIXIR"
                else:
                    log.info(
                        "[UPGRADE WALL] Saldo Gold (%s) dan Elixir (%s) tidak cukup untuk upgrade tembok ini — beralih ke farming!",
                        f"{avail_gold:,}", f"{avail_elixir:,}"
                    )
                    self.device.tap(200, 50)
                    return upgraded

                log.info("[UPGRADE WALL] Memilih upgrade menggunakan %s (Saldo: %s) di %s (Seed %s)...", res_type, f"{(avail_gold if res_type == 'GOLD' else avail_elixir):,}", target_pt, seed)
                self.device.tap(target_pt[0], target_pt[1], jitter=3)
                self._sleep(400, 600)
                # Check modal
                img_modal = self.device.screenshot()
                if img_modal is None:
                    continue

                # Check if modal is TH-locked (e.g. "Perlu meningkatkan Balai Kota ke level 17/18")
                m_words = self.vision._ocr.recognize(img_modal[450:600, 400:1000], scale=2)
                m_text = " ".join(w.text for w in m_words).lower()
                if any(k in m_text for k in ["perlu", "balai kota ke level", "perhatian!"]):
                    log.info(
                        "[UPGRADE WALL] Tembok di seed %s sudah MAX level untuk Balai Kota saat ini ('%s') — menutup modal dan mencari tembok lain...",
                        seed, m_text[:40]
                    )
                    self.device.tap(1127, 65)  # close X
                    self._sleep(200, 300)
                    self.device.tap(200, 50)
                    continue
                # Check if cost exceeds current resource balance
                from cocbot.ocr import _parse_digits
                cost_crop = img_modal[580:650, 750:950]
                cost_text = self.vision._ocr.read_text(cost_crop, scale=2)
                wall_cost = _parse_digits(cost_text) or 2_000_000
                needed_bal = avail_gold if res_type == "GOLD" else avail_elixir

                if needed_bal < wall_cost:
                    log.info(
                        "[UPGRADE WALL] Biaya tembok (%s %s) melebihi saldo (%s) — menutup modal dan beralih ke farming!",
                        f"{wall_cost:,}", res_type, f"{needed_bal:,}"
                    )
                    self.device.tap(1127, 65)  # close X
                    self._sleep(200, 300)
                    self.device.tap(200, 50)
                    if res_type == "GOLD":
                        avail_gold = 0
                    else:
                        avail_elixir = 0
                    if max(avail_gold, avail_elixir) < min_upgrade_threshold:
                        return upgraded
                    continue

                # Modal allows upgrade! Find confirm button
                pt_conf = self.vision.find_template(img_modal, "btn_confirm_modal", threshold=0.75, region=(700, 540, 1050, 670))
                if pt_conf is None:
                    pt_conf = self.vision.find_button(img_modal, "tingkatkan", region=(700, 540, 1000, 670)) or (875, 610)

                if pt_conf:
                    log.info("[UPGRADE WALL] Menekan tombol Konfirmasi di %s...", pt_conf)
                    self.device.tap(pt_conf[0], pt_conf[1], jitter=3)
                    self._sleep(400, 600)
                    upgraded += 1
                    if res_type == "GOLD":
                        avail_gold = max(0, avail_gold - wall_cost)
                    else:
                        avail_elixir = max(0, avail_elixir - wall_cost)
                    log.info("[UPGRADE WALL] Tembok #%d berhasil di-upgrade! (Sisa Gold: %s, Elixir: %s)", upgraded, f"{avail_gold:,}", f"{avail_elixir:,}")
                    seed_idx = (seed_idx + attempt + 1) % len(self._WALL_SEEDS)
                    wall_upgraded_this_round = True
                    break
                else:
                    self.device.tap(1127, 65)  # close X
                    self.device.tap(200, 50)
                    continue

            if not wall_upgraded_this_round:
                log.info("[UPGRADE WALL] Tidak ada tembok yang belum max / dapat di-upgrade di peta desa saat ini.")
                break
        # Final cleanup deselect
        self.device.tap(200, 50, jitter=2)
        elapsed = time.time() - t_start
        log.info("[UPGRADE WALL] Sesi selesai: %d tembok di-upgrade dalam %.1fs (%.1fs/tembok)", upgraded, elapsed, elapsed / max(upgraded, 1))
        return upgraded

    def _find_wall_in_panel(self, img: np.ndarray) -> dict | None:
        """Find the wall entry directly by reading panel words (exact OCR bounding box).

        Eliminates heuristic color-profiling drift which could misalign and tap
        the neighboring building row (e.g. Penyimpanan Eliksir).
        """
        from cocbot.ocr import _parse_digits

        x1, y1, x2, y2 = _BUILD_MENU_REGION
        panel_crop = img[y1:y2, x1:x2]
        if panel_crop.size == 0:
            return None

        words = self.vision._ocr.recognize(panel_crop, scale=2)
        if not words:
            return None

        for w in words:
            raw_lower = w.text.lower()
            if any(k in raw_lower for k in ["tembok", "tepabok", "teeabok", "tepbok", "dinding", "wall"]):
                # Exact center Y of the word "Tembok" inside the panel
                row_screen_y = y1 + w.y + w.h // 2

                # Read cost from the cost column of this exact row
                cost_crop = img[max(0, row_screen_y - 8):min(720, row_screen_y + 25), 1140:1265]
                cost_words = self.vision._ocr.recognize(cost_crop, scale=2)
                cost_str = "".join(cw.text for cw in cost_words)
                cost_num = _parse_digits(cost_str) or 2_000_000

                log.info(
                    "[UPGRADE WALL] Menemukan baris tembok: '%s' tepat di Y=%d (Estimasi Biaya: %s)",
                    w.text, row_screen_y, f"{cost_num:,}"
                )
                return {
                    "name": w.text,
                    "cost": cost_num,
                    "type": "gold",
                    "category": "wall",
                    "position": (1060, row_screen_y),
                }

        return None
    def _dismiss_panel(self) -> None:
        """Close the build suggestion panel by tapping the left side of the screen."""
        self.device.tap(300, 360, jitter=10)
        self._sleep(400, 600)
    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_build_menu(self, img: object) -> list[dict]:
        """Read upgrade suggestions from the build-suggestion panel via OCR.

        Returns a list of dicts: ``{"name": str, "cost": int, "type": str,
        "category": str, "position": (x, y)}``.
        """
        from cocbot.ocr import _parse_digits

        x1, y1, x2, y2 = _BUILD_MENU_REGION
        panel = img[y1:y2, x1:x2]

        words = self.vision._ocr.recognize(panel, scale=2)
        if not words:
            return []

        # Split words by horizontal position in panel coordinates:
        # Name column: 40 <= w.x < 245 (screen X = 980..1185)
        # Cost column (exclude resource icon at w.x >= 312): 240 <= w.x < 312
        name_words = [w for w in words if 40 <= w.x < 245]
        right_words = [w for w in words if 240 <= w.x < 312]

        # Group Name column words into rows by Y proximity (<= 14px)
        rows: list[list] = []
        for w in sorted(name_words, key=lambda w: (w.y, w.x)):
            if not rows or abs(w.y - rows[-1][0].y) > 14:
                rows.append([w])
            else:
                rows[-1].append(w)

        skip_headers = (
            "saran",
            "peningkatan",
            "pondok",
            "pond",
            "p0nd",
            "tukang",
            "baru",
            "mantra",
            "rnantra",
            "dahsyat",
            "dahsyab",
            "menantimu",
            "menanbimu",
            "pahlawan",
            "penuh",
        )

        suggestions: list[dict] = []
        for row in rows:
            row_sorted = sorted(row, key=lambda w: w.x)
            raw_name = " ".join(w.text for w in row_sorted).strip()
            norm_name = _normalize(raw_name)
            if not norm_name or len(norm_name) < 3:
                continue
            if any(h in norm_name or _fuzzy_match(raw_name, h) for h in skip_headers):
                continue

            row_y = sum(w.y for w in row_sorted) / len(row_sorted)

            # Collect Right-column cost words sitting below the name line (row_y + 8 .. row_y + 32)
            cost_words = [
                rw for rw in right_words
                if (row_y + 8) <= rw.y <= (row_y + 32)
            ]
            cost_words.sort(key=lambda rw: rw.x)
            # Strip trailing 'e' or 'o' if attached to '500e' from coin icon
            cleaned_parts = []
            for rw in cost_words:
                txt = rw.text
                if len(cost_words) == 1 and len(txt) == 4 and txt.lower().startswith("s") and txt[1:].lower() in ("oo0", "ooo", "000", "ooe", "00e"):
                    txt = txt[:3]
                elif len(txt) >= 4 and txt.lower().endswith("e"):
                    txt = txt[:-1]
                cleaned_parts.append(txt)
            cost_str = "".join(cleaned_parts)
            if cost_str.lower() in ("ooo", "000"):
                cost_num = 1000
            else:
                cost_num = _parse_digits(cost_str) or 0

            category = self._categorize(raw_name)
            suggestions.append({
                "name": raw_name,
                "cost": cost_num,
                "type": "gold",
                "category": category,
                "position": (1060, int(row_y) + y1 + 8),
            })

        log.debug("Build menu suggestions: %s", suggestions)
        return suggestions

    def _categorize(self, name: str) -> str:
        """Map a building name to a priority category using normalized fuzzy matching."""
        lower = name.lower()
        for cat, keywords in _CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in lower or _fuzzy_match(lower, kw):
                    return cat
        return "other"

    def _pick_upgrade(
        self,
        suggestions: list[dict],
        resources: Resources,
    ) -> dict | None:
        """Filter by reserve and sort by config priority. Return best or None."""
        cfg = self.config.upgrade
        keep_gold = getattr(cfg, "keep_reserve", {}).get("gold", 0)
        keep_elixir = getattr(cfg, "keep_reserve", {}).get("elixir", 0)

        available_gold = max(0, (getattr(resources, "gold", 0) or 0) - keep_gold)
        available_elixir = max(0, (getattr(resources, "elixir", 0) or 0) - keep_elixir)

        # Filter: can we afford it?
        max_avail = max(available_gold, available_elixir)
        affordable = [
            s for s in suggestions
            if (0 < s["cost"] <= max_avail) or (s["cost"] == 0 and max_avail >= 10_000)
        ]
        if not affordable:
            return None

        sort_by = getattr(cfg, "sort_by", "priority")
        def _cheap_key(s: dict) -> int:
            return s["cost"] if s["cost"] > 0 else 999_999

        if sort_by == "resource":
            cat_filtered = [s for s in affordable if s["category"] in ("storage", "collector")]
            if cat_filtered:
                cat_filtered.sort(key=_cheap_key)
                return cat_filtered[0]
            log.info("Mode 'resource': no storage or collector available to upgrade")
            return None
        elif sort_by == "defense":
            cat_filtered = [s for s in affordable if s["category"] == "defense"]
            if cat_filtered:
                cat_filtered.sort(key=_cheap_key)
                return cat_filtered[0]
            log.info("Mode 'defense': no defense buildings available to upgrade")
            return None
        elif sort_by == "army":
            cat_filtered = [s for s in affordable if s["category"] in ("army", "hero")]
            if cat_filtered:
                cat_filtered.sort(key=_cheap_key)
                return cat_filtered[0]
            log.info("Mode 'army': no army buildings available to upgrade")
            return None
        elif sort_by == "expensive":
            affordable.sort(key=lambda s: s["cost"], reverse=True)
            return affordable[0]
        else:  # "cheapest" or fallback
            affordable.sort(key=_cheap_key)
            return affordable[0]

    def _confirm_upgrade(self, pick: dict) -> bool:
        """Complete COC building upgrade (camera- and panel-state independent):
        1. Tap suggestion item in panel.
        2. If 'Konfirmasi' modal opened directly, tap 'Konfirmasi' at (860, 605).
           Otherwise locate the rightmost bottom action card via `_find_action_cards` (or OCR)
           -> tap 'Tingkatkan' -> tap modal 'Konfirmasi' at (860, 605).
        """
        pos = pick.get("position")
        if not pos:
            log.warning("No position for upgrade item")
            return False

        # Step 1: Select building from suggestion panel
        self.device.tap(pos[0], pos[1], jitter=4)
        self._sleep(850, 1150)

        img_step2 = self.device.screenshot()
        if self.vision.has_text(img_step2, ["perhatian", "perlu", "balai kota ke level"], region=(250, 150, 1030, 550)):
            log.info("Building '%s' requires higher Town Hall level — skipping", pick.get("name"))
            self.device.tap(200, 50, jitter=2)
            self._sleep(400, 600)
            return False

        # Check if 'Konfirmasi' modal is already open from tapping the panel item
        btn_direct = self.vision.find_button(img_step2, "konfirmasi", region=(750, 550, 960, 660))
        if btn_direct is not None:
            self.device.tap(*btn_direct, jitter=4)
            self._sleep(700, 1000)
            self.device.tap(200, 50)
            log.info("Upgrade confirmed (direct modal): %s", pick.get("name"))
            return True

        # Step 2: Tap bottom-bar 'Tingkatkan' button (rightmost action card)
        cards = self._find_action_cards(img_step2)
        if cards:
            btn_bar = (cards[-1][2], 575)
        else:
            btn_bar = self.vision.find_button(img_step2, "tingkatkan", region=(350, 500, 900, 660)) or (640, 575)
        self.device.tap(*btn_bar, jitter=4)
        self._sleep(850, 1150)

        # Step 3: Confirm inside 'Tingkatkan ... ke Level X?' modal
        img_modal = self.device.screenshot()
        if self.vision.has_text(img_modal, ["perhatian", "perlu", "balai kota ke level"], region=(250, 150, 1030, 550)):
            log.info("Building '%s' is at max level for current TH — closing modal", pick.get("name"))
            self.device.tap(200, 50, jitter=2)
            self._sleep(400, 600)
            return False

        btn_conf = self.vision.find_button(img_modal, "konfirmasi", region=(750, 550, 960, 660))
        if btn_conf is None:
            btn_conf = self.vision.find_button(img_modal, "oke", region=(600, 380, 900, 540))

        self.device.tap(*(btn_conf or (860, 605)), jitter=4)
        self._sleep(700, 1000)
        self.device.tap(200, 50)
        log.info("Upgrade confirmed: %s", pick.get("name"))
        return True

    def _sleep(self, min_ms: int, max_ms: int) -> None:
        ms = random.randint(min_ms, max_ms)
        time.sleep(ms / 1000.0)
