"""Profile configuration — Pydantic v2 models + YAML persistence."""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel

from cocbot.paths import profiles_dir


class StopWhen(BaseModel):
    rule: Literal["any", "all"] = "any"
    gold: int | str = "full"
    elixir: int | str = "full"
    dark: int | str = 0


class SearchConfig(BaseModel):
    min_th: int = 0
    min_gold: int = 1
    min_elixir: int = 1
    min_dark: int = 1
    rule: Literal["gold_and_elixir", "any", "all", "total"] = "any"
    total_min: int = 0
    relax_after_next: int = 10
    relax_percent: int = 50
    max_next: int = 60


class ArmyConfig(BaseModel):
    source: Literal["current"] = "current"
    deploy: Literal["spread_sides", "one_side", "circle"] = "spread_sides"
    sides: int = 2
    use_heroes: bool = True
    use_spells: bool = False


class UpgradeConfig(BaseModel):
    enabled: bool = True
    sort_by: Literal["priority", "cheapest", "expensive", "wall", "resource", "defense", "army"] = "priority"
    keep_reserve: dict[str, int] = {"gold": 100_000, "elixir": 100_000}
    priority: list[str] = [
        "storage",
        "collector",
        "army",
        "defense",
        "hero",
        "wall",
    ]
    walls_when_resource_over: float = 0.9

class HumanizeConfig(BaseModel):
    session_minutes: list[int] = [60, 150]
    break_minutes: list[int] = [15, 45]
    tap_jitter_px: int = 6
    delay_ms: list[int] = [120, 450]


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class Profile(BaseModel):
    name: str
    th: int = 9
    mode: Literal["classic"] = "classic"
    stop_when: StopWhen = StopWhen()
    search: SearchConfig = SearchConfig()
    army: ArmyConfig = ArmyConfig()
    upgrade: UpgradeConfig = UpgradeConfig()
    humanize: HumanizeConfig = HumanizeConfig()
    telegram: TelegramConfig = TelegramConfig()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def active_profile_path():
    return profiles_dir().parent / "profile.yaml"


def _profile_path(name: str):
    if name in ("active", "current", "default") or not name:
        if active_profile_path().exists():
            return active_profile_path()
    path = profiles_dir() / f"{name}.yaml"
    if path.exists():
        return path
    if active_profile_path().exists():
        return active_profile_path()
    return path


def load_profile(name: str = "active") -> Profile:
    """Load the single active profile YAML. Raises FileNotFoundError if missing."""
    path = _profile_path(name)
    if not path.exists():
        existing = list(profiles_dir().glob("*.yaml"))
        if existing:
            path = existing[0]
        else:
            raise FileNotFoundError(f"No profile found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        data = {}
    data.setdefault("name", name if name not in ("active", "current") else "active")
    return Profile.model_validate(data)


def save_profile(*args, **kwargs) -> None:
    """Write the single active profile to YAML.

    Accepts both ``save_profile(p)`` and ``save_profile(name, p)`` to prevent
    argument mismatch errors.
    """
    if len(args) == 1 and isinstance(args[0], Profile):
        p = args[0]
    elif len(args) >= 2 and isinstance(args[1], Profile):
        p = args[1]
        if isinstance(args[0], str):
            p.name = args[0]
    elif "p" in kwargs:
        p = kwargs["p"]
    elif "profile" in kwargs:
        p = kwargs["profile"]
    else:
        raise ValueError("Invalid arguments to save_profile")

    data = p.model_dump(mode="json")
    # Save to main active profile.yaml
    with open(active_profile_path(), "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    # Also keep named copy in profiles/ if a specific name is set
    if p.name and p.name not in ("active", "current"):
        named_path = profiles_dir() / f"{p.name}.yaml"
        with open(named_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
def list_profiles() -> list[str]:
    """Return names of all saved profiles (without extension)."""
    return sorted(p.stem for p in profiles_dir().glob("*.yaml"))


def default_profile(name: str = "default", th: int = 9) -> Profile:
    """Return a Profile with settings scaled to Town Hall level."""
    if th <= 5:
        gold_min = 35_000
        elixir_min = 35_000
        reserve = 15_000
    elif th <= 7:
        gold_min = 80_000
        elixir_min = 80_000
        reserve = 30_000
    elif th <= 9:
        gold_min = 150_000
        elixir_min = 150_000
        reserve = 60_000
    else:
        gold_min = 250_000
        elixir_min = 250_000
        reserve = 100_000

    p = Profile(name=name, th=th)
    p.search.min_gold = gold_min
    p.search.min_elixir = elixir_min
    p.upgrade.keep_reserve = {"gold": reserve, "elixir": reserve}
    return p
