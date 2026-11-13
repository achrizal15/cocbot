"""Lokasi data COCBOT. Semua config, kalibrasi, dan log berada di satu folder data."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    path = Path(os.environ.get("COCBOT_HOME", ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_dir() -> Path:
    path = data_dir() / "profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def calib_dir(width: int, height: int) -> Path:
    path = data_dir() / "calibration" / f"{width}x{height}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
