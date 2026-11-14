"""WinRT OCR wrapper for Clash of Clans text recognition."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import cv2
import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import (
    BitmapAlphaMode,
    BitmapPixelFormat,
    SoftwareBitmap,
)
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter


@dataclass(slots=True)
class Word:
    """Single recognized word with bounding box in original image coordinates."""

    text: str
    x: int
    y: int
    w: int
    h: int


# Character substitutions for COC's decorative font
_DIGIT_MAP = str.maketrans({
    "O": "0",
    "o": "0",
    "Q": "0",
    "D": "0",
    "B": "8",
    "l": "1",
    "I": "1",
    "i": "1",
    "|": "1",
    "!": "1",
    "S": "5",
    "s": "5",
    "Z": "2",
    "z": "2",
    "G": "6",
    "g": "9",
    "b": "6",
    "T": "7",
    "A": "4",
    " ": "",
    ",": "",
    ".": "",
})


import concurrent.futures

_OCR_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_OCR_LOOP = asyncio.new_event_loop()


def _run_in_ocr_loop(coro):
    asyncio.set_event_loop(_OCR_LOOP)
    return _OCR_LOOP.run_until_complete(coro)


def _run_async(coro):
    """Run a coroutine on the dedicated OCR event loop thread with timeout."""
    return _OCR_POOL.submit(_run_in_ocr_loop, coro).result(timeout=10)


class OCR:
    """WinRT OCR engine wrapper optimised for COC screen reading."""

    def __init__(self, lang: str = "en-US") -> None:
        language = Language(lang)
        self._engine = OcrEngine.try_create_from_language(language)
        if self._engine is None:
            raise RuntimeError(
                f"OCR language '{lang}' not available. "
                "Install the language pack in Windows Settings."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_bitmap(bgr: np.ndarray) -> SoftwareBitmap:
        """Convert a BGR numpy image to a WinRT SoftwareBitmap (BGRA8)."""
        h, w = bgr.shape[:2]
        # Ensure BGRA
        if bgr.ndim == 2:
            bgra = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGRA)
        elif bgr.shape[2] == 3:
            bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        else:
            bgra = bgr

        bmp = SoftwareBitmap(
            BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED
        )
        writer = DataWriter()
        writer.write_bytes(bgra.tobytes())
        bmp.copy_from_buffer(writer.detach_buffer())
        return bmp

    # ------------------------------------------------------------------
    # Recognition
    # ------------------------------------------------------------------

    async def _recognize(self, bgr: np.ndarray, scale: int = 2) -> list[Word]:
        """Async: resize image by *scale*, run OCR, return Words in original coords."""
        if scale != 1:
            bgr = cv2.resize(
                bgr,
                (bgr.shape[1] * scale, bgr.shape[0] * scale),
                interpolation=cv2.INTER_CUBIC,
            )

        bmp = self._make_bitmap(bgr)
        result = await self._engine.recognize_async(bmp)

        words: list[Word] = []
        for line in result.lines:
            for w in line.words:
                r = w.bounding_rect
                words.append(
                    Word(
                        text=w.text,
                        x=int(r.x / scale),
                        y=int(r.y / scale),
                        w=int(r.width / scale),
                        h=int(r.height / scale),
                    )
                )
        return words

    def recognize(self, bgr: np.ndarray | None, scale: int = 2) -> list[Word]:
        """Sync wrapper — recognise text in a BGR image."""
        if bgr is None or getattr(bgr, "size", 0) == 0:
            return []
        return _run_async(self._recognize(bgr, scale))

    def read_text(self, bgr: np.ndarray, scale: int = 2) -> str:
        """Return all recognized text as a single string."""
        words = self.recognize(bgr, scale)
        return " ".join(w.text for w in words)

    # ------------------------------------------------------------------
    # Digit reading (resource panels)
    # ------------------------------------------------------------------

    def read_digits(
        self, bgr: np.ndarray, region: tuple[int, int, int, int]
    ) -> int | None:
        """Crop *region* (x1, y1, x2, y2), OCR, return integer.

        Uses binary-mask OCR as an anchor, then raw-crop OCR at two
        scales to recover digits lost at comma/space separators.
        """
        if bgr is None or getattr(bgr, "size", 0) == 0:
            return None
        x1, y1, x2, y2 = region
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return None

        # Strategy 1: binary mask (white/cream text on dark background)
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = (hsv[:, :, 1] < 90) & (hsv[:, :, 2] > 200)
        binary = np.where(mask, 0, 255).astype(np.uint8)
        binary = cv2.resize(
            binary,
            (binary.shape[1] * 3, binary.shape[0] * 3),
            interpolation=cv2.INTER_NEAREST,
        )
        binary = cv2.copyMakeBorder(
            binary, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255
        )
        bgr_bin = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
        words = self.recognize(bgr_bin, scale=1)
        anchor = _parse_digits("".join(w.text for w in words))

        # Strategy 2: raw crop OCR at scale 2 and 3
        raw_results: list[int] = []
        for s in (2, 3):
            words = self.recognize(crop, scale=s)
            val = _parse_digits("".join(w.text for w in words))
            if val is not None and val > 0:
                raw_results.append(val)

        # Filter valid candidates (CoC numbers are <= 25,000,000)
        candidates = [r for r in raw_results if 0 < r <= 25_000_000]
        if anchor is not None and 0 < anchor <= 25_000_000:
            candidates.append(anchor)

        if not candidates:
            return None

        # Prefer the candidate with the most digits (numbers only lose digits when separated by spaces/commas)
        candidates.sort(key=lambda x: (len(str(x)), x), reverse=True)
        return candidates[0]


def _parse_digits(text: str) -> int | None:
    """Extract an integer from OCR text, tolerating font misreads."""
    translated = text.translate(_DIGIT_MAP)
    digits = re.sub(r"[^0-9]", "", translated)
    if not digits:
        return None
    return int(digits)
