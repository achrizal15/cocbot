"""Screen detection and resource reading via OCR for COC bot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto

import numpy as np

from cocbot.ocr import OCR, Word, _parse_digits


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ScreenType(Enum):
    HOME = auto()
    ATTACK_MENU = auto()
    ARMY_PREP = auto()
    SEARCH = auto()          # search/scout: loot visible + "Berikutnya" (Next)
    BATTLE = auto()          # live battle: loot visible + "Total Kerusakan"
    SCOUT = SEARCH           # legacy alias
    BUILD_SUGGEST = auto()
    LOADING = auto()
    UNKNOWN = auto()


@dataclass(slots=True)
class Resources:
    gold: int = 0
    elixir: int = 0
    dark: int = 0


@dataclass(slots=True)
class Loot:
    gold: int = 0
    elixir: int = 0
    dark: int = 0


@dataclass(slots=True)
class BuilderStatus:
    free: int = 0
    total: int = 0


# ---------------------------------------------------------------------------
# Layout coordinates (1280×720, from calibration)
# ---------------------------------------------------------------------------

_RESOURCE_REGIONS = {
    "gold":   (1095, 28, 1225, 52),
    "elixir": (1095, 76, 1225, 100),
    "dark":   (1095, 124, 1225, 148),
}

_LOOT_REGIONS = {
    "gold":   (30, 85, 180, 112),
    "elixir": (30, 120, 180, 148),
    "dark":   (30, 155, 180, 185),
}


# Screen markers — values are substrings to search case-insensitively.
# Order matters: more specific screens checked first.
_SCREEN_MARKERS: list[tuple[ScreenType, list[str]]] = [
    (ScreenType.ATTACK_MENU,  ["multipe", "pemain"]),
    (ScreenType.ATTACK_MENU,  ["pemain", "tunggal"]),
    (ScreenType.ATTACK_MENU,  ["cari", "lawan"]),
    (ScreenType.ATTACK_MENU,  ["lawan", "tanding"]),
    (ScreenType.BUILD_SUGGEST, ["saran", "peningkatan"]),
    (ScreenType.BUILD_SUGGEST, ["peningkatan", "lain"]),
    (ScreenType.BATTLE,       ["total", "kerusakan"]),
    (ScreenType.BATTLE,       ["sisa", "waktu"]),
    (ScreenType.SEARCH,       ["rampasan", "berikut"]),
    (ScreenType.SEARCH,       ["rampasan", "tersedia"]),
    (ScreenType.SEARCH,       ["akhiri", "serangan"]),
    (ScreenType.SEARCH,       ["dimulai", "dalam"]),
    (ScreenType.ARMY_PREP,    ["tentaraku"]),
    (ScreenType.ARMY_PREP,    ["resep", "formasi"]),
    (ScreenType.HOME,         ["toko"]),
    (ScreenType.HOME,         ["gratis"]),
    (ScreenType.HOME,         ["bangun"]),
]

# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"[^a-z0-9]")


def _normalize(text: str) -> str:
    """Lowercase and strip non-alphanumeric characters."""
    return _STRIP_RE.sub("", text.lower())


def _edit_distance(a: str, b: str) -> int:
    """Simple Levenshtein distance (no external dep)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = curr
    return prev[-1]


def _fuzzy_match(ocr_text: str, target: str) -> bool:
    """Case-insensitive fuzzy match: substring check or edit distance check.

    Edit-distance threshold scales with target length to avoid false positives
    on short words (e.g. 'sang' matching 'serang').
    """
    norm_ocr = _normalize(ocr_text)
    norm_target = _normalize(target)
    if not norm_target:
        return False
    # Substring match on full blob
    if norm_target in norm_ocr:
        return True
    # For short words (<= 4 chars like 'cari', 'oke'), require exact substring only
    if len(norm_target) <= 4:
        return False
    max_dist = max(1, int(len(norm_target) * 0.28))
    for raw_word in ocr_text.split():
        norm_word = _normalize(raw_word)
        if norm_word and _edit_distance(norm_word, norm_target) <= max_dist:
            return True
    return False


# ---------------------------------------------------------------------------
# Pixel-color screen detection (0.01ms vs 91ms OCR)
# ---------------------------------------------------------------------------

# Each screen has distinctive pixel colors at fixed positions (1280x720).
# Format: (y, x, B, G, R, tolerance) — BGR order as cv2 uses.

def _pixel_match(img: np.ndarray, y: int, x: int, bgr: tuple[int, int, int], tol: int = 30) -> bool:
    """Check if pixel at (y, x) matches expected BGR color within tolerance."""
    if y < 0 or y >= img.shape[0] or x < 0 or x >= img.shape[1]:
        return False
    px = img[y, x]
    return (abs(int(px[0]) - bgr[0]) + abs(int(px[1]) - bgr[1]) + abs(int(px[2]) - bgr[2])) <= tol


def _detect_screen_fast(img: np.ndarray) -> ScreenType | None:
    """Try to detect screen via pixel color checks only (< 0.01ms).

    Returns ``None`` if no confident match — caller should fall back to OCR.
    """
    h, w = img.shape[:2]
    if h < 700 or w < 1200:
        return None

    # BUILD_SUGGEST: panel left-edge divider at x=935 is pitch black (0,0,0)
    # when panel is open. Check multiple Y positions for robustness.
    if (img[300, 935].max() < 10
            and img[200, 935].max() < 10
            and img[400, 935].max() < 10):
        return ScreenType.BUILD_SUGGEST

    # ATTACK_MENU: modal on left half has large orange "Cari Lawan Tanding" button
    # at Y=530, X=220..360 with distinctive BGR ~(44, 173, 249)
    if (_pixel_match(img, 530, 290, (44, 173, 249), tol=35)
            or _pixel_match(img, 530, 360, (44, 173, 249), tol=35)):
        return ScreenType.ATTACK_MENU

    # HOME: attack button bottom-left is bright gold/orange (B~80, G~243, R~255)
    # and no attack menu or builder panel modal is open
    if _pixel_match(img, 650, 90, (80, 243, 255), tol=40):
        # Verify it's not attack menu
        if not _pixel_match(img, 530, 290, (44, 173, 249), tol=35):
            return ScreenType.HOME

    return None


def _has_color_in_region(
    img: np.ndarray,
    region: tuple[int, int, int, int],
    bgr: tuple[int, int, int],
    tol: int = 30,
) -> bool:
    """Check if any pixel in *region* (x1, y1, x2, y2) matches *bgr* within *tol*."""
    x1, y1, x2, y2 = region
    section = img[y1:y2, x1:x2]
    if section.size == 0:
        return False
    diff = np.abs(section.astype(np.int16) - np.array(bgr, dtype=np.int16)).sum(axis=2)
    return bool(np.any(diff <= tol))


def find_panel_entries(img: np.ndarray) -> list[int]:
    """Detect upgrade entry Y positions in the builder panel via color profiling.

    Returns a list of screen-Y coordinates for each detected entry center.
    Works by detecting pairs of white text bands (name + cost lines).
    Takes < 1ms vs 57ms for OCR-based ``_read_build_menu``.
    """
    panel_crop = img[70:645, 940:1280]
    if panel_crop.size == 0:
        return []

    # White pixels = upgrade name and cost text
    white_mask = np.all(panel_crop > 230, axis=2)
    profile = white_mask.mean(axis=1)

    # Find contiguous bands where white text is present
    threshold = 0.015
    bands: list[tuple[int, int]] = []
    in_band = False
    start = 0
    for i, val in enumerate(profile):
        if val > threshold and not in_band:
            in_band = True
            start = i
        elif val <= threshold and in_band:
            in_band = False
            bands.append((start, i))
    if in_band:
        bands.append((start, len(profile)))

    # Pair consecutive bands that are close together (< 30px) = one entry
    entries: list[int] = []
    i = 0
    while i < len(bands) - 1:
        center1 = (bands[i][0] + bands[i][1]) // 2
        center2 = (bands[i + 1][0] + bands[i + 1][1]) // 2
        if center2 - center1 < 30:
            # Name + cost pair: entry center is between them
            entries.append((center1 + center2) // 2 + 70)  # +70 for crop offset
            i += 2
        else:
            i += 1

    return entries

# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------

class Vision:
    """High-level screen analysis: detection, resource/loot reading."""

    def __init__(self) -> None:
        self._ocr = OCR()
        self._templates: dict[str, np.ndarray] = {}
        self._preload_templates()

    def _preload_templates(self) -> None:
        """Preload button templates from cocbot/assets/templates/ into memory."""
        from pathlib import Path
        import cv2
        tmpl_dir = Path(__file__).parent / "assets" / "templates"
        if tmpl_dir.exists():
            for p in tmpl_dir.glob("*.png"):
                img = cv2.imread(str(p))
                if img is not None:
                    self._templates[p.stem] = img
    # ------------------------------------------------------------------
    # Screen detection
    # ------------------------------------------------------------------

    def detect_screen(self, img: np.ndarray) -> ScreenType:
        """Identify current game screen.

        Fast path: pixel-color checks (< 0.1ms).
        Slow path: OCR marker words (fallback).
        """
        if img is None or getattr(img, "size", 0) == 0:
            return ScreenType.LOADING

        # --- fast pixel-color detection (<0.1ms) ---
        fast = _detect_screen_fast(img)
        if fast is not None:
            return fast

        # --- OCR fallback (91ms) ---
        # Check right-side builder panel crop first
        panel_crop = img[0:720, 940:1280]
        if panel_crop.size > 0:
            panel_words = self._ocr.recognize(panel_crop, scale=2)
            panel_blob = " ".join(w.text for w in panel_words)
            if (
                (_fuzzy_match(panel_blob, "saran") and _fuzzy_match(panel_blob, "peningkatan"))
                or (_fuzzy_match(panel_blob, "bangunan") and _fuzzy_match(panel_blob, "pahlawan"))
                or (_fuzzy_match(panel_blob, "peningkatan") and _fuzzy_match(panel_blob, "lain"))
            ):
                return ScreenType.BUILD_SUGGEST

        words = self._ocr.recognize(img, scale=1)
        if not words:
            words = self._ocr.recognize(img, scale=2)
        if not words:
            return ScreenType.LOADING
        blob = " ".join(w.text for w in words)

        for screen_type, markers in _SCREEN_MARKERS:
            matched = sum(1 for m in markers if _fuzzy_match(blob, m))
            if matched >= len(markers):
                return screen_type
        if any(_fuzzy_match(blob, k) for k in ["toko", "gratis", "bangun"]):
            return ScreenType.HOME
        if _fuzzy_match(blob, "pertempuran") and not _fuzzy_match(blob, "cari"):
            return ScreenType.HOME

        # Corner crop fallback
        for crop in (img[635:718, 10:165], img[635:718, 1095:1275], img[340:500, 1095:1275]):
            if crop.size > 0:
                c_words = self._ocr.recognize(crop, scale=2)
                c_text = " ".join(w.text for w in c_words).lower()
                if any(k in c_text for k in ["pertempuran", "pentempunan", "pertempun", "bangun", "toko", "gratis"]):
                    return ScreenType.HOME
        return ScreenType.UNKNOWN

    # ------------------------------------------------------------------
    # Resource reading (home screen top-right)
    # ------------------------------------------------------------------

    def read_resources(self, img: np.ndarray) -> Resources:
        """Read gold / elixir / dark from the resource panel (Home or BUILD_SUGGEST)."""
        if img is None or getattr(img, "size", 0) == 0:
            return Resources()

        # When BUILD_SUGGEST panel is open on the right, resource bars shift left by ~350px (X=730..885)
        # Use fast pixel check instead of full detect_screen (saves ~91ms)
        is_panel = _detect_screen_fast(img) == ScreenType.BUILD_SUGGEST
        if is_panel:
            regions = {
                "gold":   (730, 28, 885, 52),
                "elixir": (730, 76, 885, 100),
                "dark":   (730, 124, 885, 148),
            }
            x_min, x_max = 710, 900
        else:
            regions = _RESOURCE_REGIONS
            x_min, x_max = 1080, 1260

        gold = self._ocr.read_digits(img, regions["gold"])
        elixir = self._ocr.read_digits(img, regions["elixir"])
        dark = self._ocr.read_digits(img, regions["dark"])

        # Fallback: parse directly from OCR words in the resource bar region
        if gold is None or elixir is None or gold == 0 or elixir == 0:
            bar_crop = img[15:160, x_min:x_max]
            words = self._ocr.recognize(bar_crop, scale=2)
            g_w = [w.text for w in words if 5 < (w.y + w.h // 2) < 42]
            e_w = [w.text for w in words if 50 < (w.y + w.h // 2) < 92]
            d_w = [w.text for w in words if 100 < (w.y + w.h // 2) < 142]
            if gold is None or gold == 0:
                gold = _parse_digits("".join(g_w))
            if elixir is None or elixir == 0:
                elixir = _parse_digits("".join(e_w))
            if dark is None or dark == 0:
                dark = _parse_digits("".join(d_w))

        return Resources(
            gold=min(gold or 0, 20_000_000),
            elixir=min(elixir or 0, 20_000_000),
            dark=min(dark or 0, 500_000),
        )
    # ------------------------------------------------------------------
    # Loot reading (scout screen top-left)
    # ------------------------------------------------------------------

    def read_loot(self, img: np.ndarray) -> Loot:
        """Read available loot during the scout / search phase with high precision."""
        if img is None or getattr(img, "size", 0) == 0:
            return Loot()

        regions = {
            "gold":   (40, 78, 235, 120),
            "elixir": (40, 118, 235, 160),
            "dark":   (40, 158, 235, 200),
        }

        mapping = {
            "o": "0", "O": "0", "D": "0", "Q": "0",
            "I": "1", "l": "1", "i": "1", "|": "1", "!": "1",
            "Z": "2", "z": "2", "B": "8", "S": "5", "s": "5",
            "T": "7", "G": "6", "b": "6", "A": "4",
        }

        def parse_crop(crop: np.ndarray) -> int:
            if crop.size == 0:
                return 0
            candidates: list[int] = []
            for s in (1, 2):
                ws = self._ocr.recognize(crop, scale=s)
                if not ws:
                    continue
                sorted_ws = sorted(ws, key=lambda w: w.x)
                cleaned: list[str] = []
                for w in sorted_ws:
                    for ch in w.text:
                        if ch.isdigit():
                            cleaned.append(ch)
                        elif ch in mapping:
                            cleaned.append(mapping[ch])
                digits_str = "".join(cleaned).lstrip("0")
                if digits_str:
                    try:
                        v = int(digits_str)
                        if 0 < v <= 25_000_000:
                            candidates.append(v)
                    except ValueError:
                        pass
            if not candidates:
                return 0
            # Prefer candidate with longest digit string
            candidates.sort(key=lambda x: len(str(x)), reverse=True)
            return candidates[0]

        return Loot(
            gold=parse_crop(img[regions["gold"][1]:regions["gold"][3], regions["gold"][0]:regions["gold"][2]]),
            elixir=parse_crop(img[regions["elixir"][1]:regions["elixir"][3], regions["elixir"][0]:regions["elixir"][2]]),
            dark=parse_crop(img[regions["dark"][1]:regions["dark"][3], regions["dark"][0]:regions["dark"][2]]),
        )
    # ------------------------------------------------------------------
    # Builder status
    # ------------------------------------------------------------------

    def read_builder_status(self, img: np.ndarray) -> BuilderStatus:
        """Read builder text like '2/2' or '1/2' from right-side builder area or panel tab."""
        if img is None or getattr(img, "size", 0) == 0:
            return BuilderStatus()

        def _parse_badge(text: str) -> BuilderStatus | None:
            t = text.replace(" ", "").replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
            m = re.search(r"(\d+)/(\d+)", t)
            if m:
                return BuilderStatus(free=int(m.group(1)), total=int(m.group(2)))
            # Handle '112' -> '1/2' or '212' -> '2/2' when '/' is read as '1'
            m3 = re.fullmatch(r"([0-6])1([2-6])", t)
            if m3 and int(m3.group(1)) <= int(m3.group(2)):
                return BuilderStatus(free=int(m3.group(1)), total=int(m3.group(2)))
            return None

        for y1, y2, x1, x2 in [
            (630, 720, 935, 1025),  # BUILD_SUGGEST bottom-left builder tab (X=935..1025, Y=630..720)
            (500, 720, 1080, 1280), # HOME screen builder badge above Bangun
            (480, 620, 1050, 1250),
        ]:
            crop = img[y1:y2, x1:x2]
            if crop.size > 0:
                for s in (2, 3, 4):
                    for w in self._ocr.recognize(crop, scale=s):
                        res = _parse_badge(w.text)
                        if res is not None:
                            return res
        words = self._ocr.recognize(img, scale=2)
        for w in words:
            if w.x < 935:
                continue
            res = _parse_badge(w.text)
            if res is not None:
                return res
        return BuilderStatus()

    # ------------------------------------------------------------------
    # Auto-detection (profile from game state)
    # ------------------------------------------------------------------

    def read_player_name(self, img: np.ndarray) -> str:
        """Read the in-game account player name from top-left area."""
        blocked = {"sang", "pendendam", "clan"}
        # Crop name box with margin above the clan name line at Y=78: X=100..280, Y=40..78
        crop = img[40:78, 100:280]
        if crop.size > 0:
            for s in (2, 3):
                words = self._ocr.recognize(crop, scale=s)
                filtered = [
                    re.sub(r"[^\w-]", "", w.text)
                    for w in words
                    if w.y < 30 and re.sub(r"[^\w-]", "", w.text).lower() not in blocked
                ]
                clean = " ".join(w for w in filtered if w).strip()
                if clean:
                    return clean

        # Fallback to full-image words
        words = self._ocr.recognize(img, scale=2)
        name_words = [
            re.sub(r"[^\w-]", "", w.text)
            for w in words
            if 90 < (w.x + w.w // 2) < 320
            and 45 < (w.y + w.h // 2) < 72
            and re.sub(r"[^\w-]", "", w.text).lower() not in blocked
        ]
        name = " ".join(w for w in name_words if w).strip()
        return name or "Player"

    def detect_th_level(
        self, img: np.ndarray, device: Any | None = None
    ) -> int:
        """Detect Town Hall level non-destructively without triggering Exit modal."""
        import time

        if device is not None:
            try:
                builder = self.read_builder_status(img)
                if builder.total > 0:
                    # Open Bangunan dan Pahlawan panel cleanly
                    device.tap(1142, 555)
                    time.sleep(1.0)
                    img_panel = device.screenshot()
                    if img_panel is not None and self.detect_screen(img_panel) == ScreenType.BUILD_SUGGEST:
                        ws = self._ocr.recognize(img_panel[70:645, 940:1280], scale=2)
                        blob = " ".join(w.text for w in ws)
                        # Close panel cleanly by tapping left side of village
                        device.tap(300, 360)
                        time.sleep(0.6)
                        m = re.search(
                            r"balai\s*kota.*?(\d+)",
                            blob,
                            re.IGNORECASE,
                        )
                        if m:
                            val = int(m.group(1))
                            if 2 <= val <= 17:
                                return val - 1
                    else:
                        device.tap(300, 360)
                        time.sleep(0.5)
            except Exception:
                pass

        try:
            from cocbot.config import load_profile

            prof = load_profile()
            if 1 <= getattr(prof, "th", 0) <= 17:
                return int(prof.th)
        except Exception:
            pass

        res = self.read_resources(img)
        max_res = max(res.gold, res.elixir)
        if max_res <= 500_000:
            return 4
        if max_res <= 1_000_000:
            return 6
        if max_res <= 2_000_000:
            return 7
        return 8

    def auto_detect_profile(
        self, img: np.ndarray, device: Any | None = None
    ) -> dict:
        """Detect game state, player name, and TH from home screen."""
        player_name = self.read_player_name(img)
        th = self.detect_th_level(img, device=device)
        resources = self.read_resources(img)
        builder = self.read_builder_status(img)
        return {
            "name": player_name,
            "th": th,
            "gold": resources.gold,
            "elixir": resources.elixir,
            "dark": resources.dark,
            "builder_free": builder.free,
            "builder_total": builder.total,
        }
    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def has_text(
        self,
        img: np.ndarray,
        texts: list[str],
        region: tuple[int, int, int, int] | None = None,
    ) -> bool:
        """Check if any of *texts* appear in the image (or a region of it)."""
        if region is not None:
            x1, y1, x2, y2 = region
            img = img[y1:y2, x1:x2]

        words = self._ocr.recognize(img, scale=2)
        blob = " ".join(w.text for w in words)
        return any(_fuzzy_match(blob, t) for t in texts)

    def find_template(
        self,
        img: np.ndarray,
        template_name: str,
        threshold: float = 0.80,
        region: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int] | None:
        """Find a UI template via cv2.matchTemplate (normalized cross-correlation).

        Runs in < 2ms without OCR. Returns (center_x, center_y) or None.
        """
        import cv2
        if img is None or getattr(img, "size", 0) == 0:
            return None
        tmpl = self._templates.get(template_name)
        if tmpl is None:
            from pathlib import Path
            path = Path(__file__).parent / "assets" / "templates" / f"{template_name}.png"
            if path.exists():
                tmpl = cv2.imread(str(path))
                if tmpl is not None:
                    self._templates[template_name] = tmpl
        if tmpl is None:
            return None

        offset_x, offset_y = 0, 0
        search_img = img
        if region is not None:
            x1, y1, x2, y2 = region
            search_img = img[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1

        th, tw = tmpl.shape[:2]
        sh, sw = search_img.shape[:2]
        if sh < th or sw < tw:
            return None

        res = cv2.matchTemplate(search_img, tmpl, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        if max_val >= threshold:
            cx = offset_x + max_loc[0] + tw // 2
            cy = offset_y + max_loc[1] + th // 2
            return (cx, cy)
        return None

    def find_button(
        self,
        img: np.ndarray,
        text: str,
        region: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int] | None:
        """Find a button by OCR text and return its center coordinates."""
        offset_x, offset_y = 0, 0
        if region is not None:
            x1, y1, x2, y2 = region
            img = img[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1

        words = self._ocr.recognize(img, scale=2)
        for w in words:
            if _fuzzy_match(w.text, text):
                cx = offset_x + w.x + w.w // 2
                cy = offset_y + w.y + w.h // 2
                return (cx, cy)
        return None
