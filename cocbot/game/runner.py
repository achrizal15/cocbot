"""Main bot loop — session/break cycle, stop conditions, stats."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from cocbot.adb import AdbDevice
    from cocbot.config import Profile
    from cocbot.vision import Resources, Vision

from cocbot.game.attack import Attacker
from cocbot.game.builder import Builder
from cocbot.game.navigator import Navigator

log = logging.getLogger("cocbot")

# Max storage capacity per TH (approximate, assumes max-level storages)
_TH_CAPACITY: dict[int, dict[str, int]] = {
    4:  {"gold": 500_000,    "elixir": 500_000,    "dark": 0},
    5:  {"gold": 500_000,    "elixir": 500_000,    "dark": 0},
    6:  {"gold": 1_000_000,  "elixir": 1_000_000,  "dark": 0},
    7:  {"gold": 2_000_000,  "elixir": 2_000_000,  "dark": 10_000},
    8:  {"gold": 4_000_000,  "elixir": 4_000_000,  "dark": 75_000},
    9:  {"gold": 6_000_000,  "elixir": 6_000_000,  "dark": 150_000},
    10: {"gold": 7_500_000,  "elixir": 7_500_000,  "dark": 200_000},
    11: {"gold": 8_500_000,  "elixir": 8_500_000,  "dark": 250_000},
    12: {"gold": 10_000_000, "elixir": 10_000_000, "dark": 300_000},
    13: {"gold": 12_000_000, "elixir": 12_000_000, "dark": 350_000},
    14: {"gold": 14_000_000, "elixir": 14_000_000, "dark": 400_000},
    15: {"gold": 16_000_000, "elixir": 16_000_000, "dark": 450_000},
    16: {"gold": 18_000_000, "elixir": 18_000_000, "dark": 500_000},
}


class BotRunner:
    """Orchestrates the full farming loop."""

    def __init__(
        self,
        device: AdbDevice,
        vision: Vision,
        config: Profile,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.device = device
        self.vision = vision
        self.config = config

        self.navigator = Navigator(device, vision)
        self.attacker = Attacker(device, vision, config)
        self.builder = Builder(device, vision, config)

        self.on_event = on_event

        # State
        self._running = False
        self._stop_flag = False

        # Stats
        self.total_attacks: int = 0
        self.total_gold: int = 0
        self.total_elixir: int = 0
        self.total_dark: int = 0
        self.session_start: float = 0.0
        self.last_attack: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main bot loop. Blocks until stopped or stop conditions met."""
        self._running = True
        self._stop_flag = False
        self.session_start = time.monotonic()
        self._reset_stats()

        log.info("Bot started")
        self._emit("started", {})

        try:
            self._loop()
        except Exception:
            log.exception("Bot loop crashed")
            self._emit("error", {"reason": "crash"})
        finally:
            self._running = False
            log.info(
                "Bot stopped — attacks=%d gold=%d elixir=%d dark=%d",
                self.total_attacks, self.total_gold, self.total_elixir, self.total_dark,
            )
            self._emit("stopped", self.status())

    def stop(self) -> None:
        """Signal the bot to stop after the current cycle."""
        log.info("Stop requested")
        self._stop_flag = True

    def is_running(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        """Current state and cumulative stats."""
        elapsed = time.monotonic() - self.session_start if self.session_start else 0
        return {
            "running": self._running,
            "total_attacks": self.total_attacks,
            "total_gold": self.total_gold,
            "total_elixir": self.total_elixir,
            "total_dark": self.total_dark,
            "session_seconds": int(elapsed),
            "last_attack": self.last_attack,
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_flag:
            # 1. Ensure home screen
            log.info("=" * 45)
            log.info("[SIKLUS] Memulai siklus baru bot...")
            img = self.navigator.ensure_home()

            # 2. Read resources & check attack pause condition
            resources = self.vision.read_resources(img)
            if resources:
                self._emit("resources", _res_dict(resources))
                log.info(
                    "[STATUS DESA] Gold: %s | Elixir: %s | Dark: %s",
                    f"{getattr(resources, 'gold', 0):,}",
                    f"{getattr(resources, 'elixir', 0):,}",
                    f"{getattr(resources, 'dark', 0):,}",
                )

            # Lewati pause stop_attack & builder untuk fokus 100% pada alur farming inti
            # (Step 1: Tekan tombol Pertempuran, Step 2: Validasi Cari Lawan Tanding, Step 3: Deploy All Troops)

            # Pastikan berada di Beranda sebelum memulai penyerangan
            img = self.navigator.ensure_home()
            log.info("[FARMING] Mempersiapkan penyerangan desa lawan...")
            if not self.attacker.start_attack():
                log.warning("[FARMING] Belum berhasil membuka pencarian lawan — mencoba kembali pada siklus berikutnya.")
                self._humanize_sleep(5000, 10000)
                continue

            # 7. Search for target
            loot = self.attacker.search_target()
            if loot is None:
                log.info("[FARMING] Tidak ada target yang memenuhi kriteria loot — mengakhiri pertempuran.")
                self.attacker.end_battle()
                self.navigator.go_home()
                self._humanize_sleep(10000, 20000)
                continue

            # 8. Deploy troops
            log.info("[SERANG] Menyerang base lawan! Menyebarkan pasukan...")
            self.attacker.deploy_troops()

            # 9. Wait for battle end
            log.info("[SERANG] Menunggu pertempuran selesai...")
            self.attacker.wait_battle_end()
            self.last_attack = time.monotonic()

            # 10. Record stats
            loot_g = getattr(loot, "gold", 0) or 0
            loot_e = getattr(loot, "elixir", 0) or 0
            loot_d = getattr(loot, "dark", 0) or 0
            self.total_attacks += 1
            self.total_gold += loot_g
            self.total_elixir += loot_e
            self.total_dark += loot_d

            log.info(
                "[HASIL] Serangan #%d selesai! Rampasan: Gold +%s | Elixir +%s | Dark +%s",
                self.total_attacks, f"{loot_g:,}", f"{loot_e:,}", f"{loot_d:,}",
            )

            event_payload = {
                "attack_number": self.total_attacks,
                "loot_gold": loot_g,
                "loot_elixir": loot_e,
                "loot_dark": loot_d,
                "gold": loot_g,
                "elixir": loot_e,
                "dark": loot_d,
                "total_attacks": self.total_attacks,
            }
            self._emit("attack_complete", event_payload)
            self._emit("attack_completed", event_payload)
            log.info("[BERANDA] Kembali ke Beranda Desa...")
            self.navigator.go_home()

            # 10. Session break check
            if self._should_break():
                self._session_break()

            # Small delay between cycles
            self._humanize_sleep(2000, 5000)

    # ------------------------------------------------------------------
    # Stop conditions
    # ------------------------------------------------------------------

    def _check_stop_conditions(self, resources: Resources) -> bool:
        """Evaluate ``stop_when`` config against current resources."""
        stop = self.config.stop_when
        rule = getattr(stop, "rule", "any")

        gold_target = getattr(stop, "gold", None)
        elixir_target = getattr(stop, "elixir", None)
        dark_target = getattr(stop, "dark", None)

        cur_gold = getattr(resources, "gold", 0) or 0
        cur_elixir = getattr(resources, "elixir", 0) or 0
        cur_dark = getattr(resources, "dark", 0) or 0

        th = getattr(self.config, "th", 4)

        # If wall-only mode AND user hasn't disabled stop-checks (all=0),
        # only pause attacks when Gold storage is full (ignore Elixir so bot keeps farming gold for walls).
        # If user set all stop_when to 0 → never stop.
        cfg_up = getattr(self.config, "upgrade", None)
        sort_by = getattr(cfg_up, "sort_by", "") if cfg_up else ""
        priority = getattr(cfg_up, "priority", []) if cfg_up else []
        if sort_by == "wall" or (priority and priority[0] == "wall"):
            # Only apply auto-stop if user has at least one non-zero stop target
            has_any_target = (
                (isinstance(gold_target, (int, float)) and gold_target > 0)
                or (isinstance(gold_target, str) and gold_target.strip() not in ("", "0"))
                or (isinstance(elixir_target, (int, float)) and elixir_target > 0)
                or (isinstance(elixir_target, str) and elixir_target.strip() not in ("", "0"))
            )
            if not has_any_target:
                return False  # user set all to 0 → never stop attacking
            cap_gold = _TH_CAPACITY.get(th, {}).get("gold", 500_000)
            return cur_gold >= int(cap_gold * 0.95)

        def _enabled(t: Any) -> bool:
            if t is None:
                return False
            if isinstance(t, (int, float)) and t <= 0:
                return False
            if isinstance(t, str) and t.strip() in ("", "0"):
                return False
            return True

        checks: list[bool] = []
        if _enabled(gold_target):
            checks.append(self._resource_met(cur_gold, gold_target, "gold", th))
        if _enabled(elixir_target):
            checks.append(self._resource_met(cur_elixir, elixir_target, "elixir", th))
        if _enabled(dark_target):
            checks.append(self._resource_met(cur_dark, dark_target, "dark", th))

        if not checks:
            return False

        # When both Gold and Elixir are set to 'full', require both to be full
        # so having full Elixir does not stop the bot from farming Gold (and vice versa).
        if (
            isinstance(gold_target, str)
            and gold_target.lower() == "full"
            and isinstance(elixir_target, str)
            and elixir_target.lower() == "full"
        ):
            return all(checks)

        if rule == "all":
            return all(checks)
        else:  # "any" (default)
            return any(checks)

    @staticmethod
    def _resource_met(
        current: int, target: int | str, res_type: str = "", th: int = 9
    ) -> bool:
        """Check a single resource against a target.

        ``'full'`` → >= 95% of TH capacity.
        ``0`` or ``None`` → disabled (never met).
        Positive int → >= that amount.
        """
        if target is None:
            return False
        if isinstance(target, str) and target.lower() == "full":
            cap = _TH_CAPACITY.get(th, {}).get(res_type, 0)
            if cap <= 0:
                return False
            return current >= int(cap * 0.95)
        try:
            t = int(target)
        except (ValueError, TypeError):
            return False
        if t <= 0:
            return False
        return current >= t

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _should_break(self) -> bool:
        """Check if the current session has exceeded the configured limit."""
        humanize = self.config.humanize
        session_range = getattr(humanize, "session_minutes", [60, 150])
        if not session_range or len(session_range) < 2:
            return False

        elapsed_min = (time.monotonic() - self.session_start) / 60
        # Pick a random limit within the range (decided once per check)
        limit = random.uniform(session_range[0], session_range[1])
        return elapsed_min >= limit

    def _session_break(self) -> None:
        """Sleep for a randomized break duration, then reset session timer."""
        humanize = self.config.humanize
        break_range = getattr(humanize, "break_minutes", [15, 45])
        if not break_range or len(break_range) < 2:
            break_min = 15.0
        else:
            break_min = random.uniform(break_range[0], break_range[1])

        log.info("Taking a break for %.1f minutes", break_min)
        self._emit("break_started", {"duration_minutes": round(break_min, 1)})

        time.sleep(break_min * 60)

        # Reset session timer after break
        self.session_start = time.monotonic()
        log.info("Break over — resuming")
        self._emit("break_ended", {})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_stats(self) -> None:
        self.total_attacks = 0
        self.total_gold = 0
        self.total_elixir = 0
        self.total_dark = 0
        self.last_attack = 0.0

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:
                log.exception("on_event callback failed for %s", event)

    def _humanize_sleep(self, min_ms: int, max_ms: int) -> None:
        ms = random.randint(min_ms, max_ms)
        time.sleep(ms / 1000.0)


def _res_dict(resources: Resources) -> dict[str, Any]:
    """Convert a Resources object to a plain dict for events."""
    return {
        "gold": getattr(resources, "gold", 0),
        "elixir": getattr(resources, "elixir", 0),
        "dark": getattr(resources, "dark", 0),
    }
