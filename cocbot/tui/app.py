"""Textual TUI for COCBOT — four-tab interface."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

log = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────


def _fmt_num(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"

class _TuiLogHandler(logging.Handler):
    """Routes cocbot logger messages directly into the TUI RichLog widget."""

    def __init__(self, app: CocBotApp) -> None:
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if not msg:
                return
            if record.levelno >= logging.ERROR:
                styled = f"[bold red]{msg}[/bold red]"
            elif record.levelno >= logging.WARNING:
                styled = f"[yellow]{msg}[/yellow]"
            elif "[FARMING]" in msg:
                styled = f"[bold green]{msg}[/bold green]"
            elif "[CARI TARGET" in msg:
                styled = f"[cyan]{msg}[/cyan]"
            elif "[SERANG]" in msg or "[HASIL]" in msg:
                styled = f"[bold yellow]{msg}[/bold yellow]"
            elif "[UPGRADE]" in msg or "[BUILDER]" in msg:
                styled = f"[magenta]{msg}[/magenta]"
            elif "[BERANDA]" in msg or "[STATUS DESA]" in msg:
                styled = f"[blue]{msg}[/blue]"
            elif "[SIKLUS]" in msg:
                styled = f"[bold white]{msg}[/bold white]"
            else:
                styled = msg
            self.app.call_from_thread(self.app._log, styled)
        except Exception:
            pass


# ── App ──────────────────────────────────────────────────────────


class CocBotApp(App):
    """COCBOT Terminal UI."""

    TITLE = "COCBOT"
    CSS = """
    Screen {
        layout: vertical;
    }
    #dashboard-grid {
        layout: grid;
        grid-size: 2 2;
        grid-gutter: 1;
        padding: 1;
    }
    .panel {
        border: solid $primary;
        padding: 1;
        height: auto;
    }
    .panel-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .btn-row {
        height: 3;
        dock: bottom;
        padding: 0 1;
    }
    .form-row {
        height: 3;
        margin-bottom: 0;
    }
    .form-label {
        width: 22;
        padding: 1 1 0 0;
    }
    .form-input {
        width: 1fr;
    }
    .section-title {
        text-style: bold;
        margin: 1 0 0 0;
        color: $accent;
    }
    #log-view {
        height: 1fr;
        min-height: 8;
        border: solid $primary;
    }
    .status-line {
        height: 1;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(self, profile_name: str = "default", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._profile_name = profile_name
        self._profile: Any = None
        self._adb: Any = None
        self._vision: Any = None
        self._runner: Any = None
        self._tg: Any = None
        self._status = "Stopped"
        self._stats: dict[str, Any] = {
            "attacks": 0,
            "gold": 0,
            "elixir": 0,
            "dark": 0,
            "session_start": None,
            "last_attack": None,
        }
        self._resources: dict[str, int | None] = {
            "gold": None,
            "elixir": None,
            "dark": None,
        }

    # ── compose ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent("Dashboard", "Profile", "Farming", "Upgrade", "Telegram", "Device"):
            # Tab 1: Dashboard
            with TabPane("Dashboard", id="tab-dashboard"):
                yield from self._compose_dashboard()
            # Tab 2: Profile (In-Game Verified Account)
            with TabPane("Profile", id="tab-profile"):
                yield from self._compose_profile()
            # Tab 3: Farming (Farming Rules)
            with TabPane("Farming", id="tab-farming"):
                yield from self._compose_farming()
            # Tab 4: Upgrade (Upgrade Rules)
            with TabPane("Upgrade", id="tab-upgrade"):
                yield from self._compose_upgrade()
            # Tab 5: Telegram
            with TabPane("Telegram", id="tab-telegram"):
                yield from self._compose_telegram()
            # Tab 6: Device
            with TabPane("Device", id="tab-device"):
                yield from self._compose_device()
        yield Footer()

    # ── Dashboard ────────────────────────────────────────────────

    def _compose_dashboard(self) -> ComposeResult:
        with Vertical():
            yield Static("Status: Stopped", id="dash-status", classes="status-line")
            with Horizontal():
                with Vertical(classes="panel"):
                    yield Label("Resources", classes="panel-title")
                    yield Static("Gold: —", id="res-gold")
                    yield Static("Elixir: —", id="res-elixir")
                    yield Static("Dark: —", id="res-dark")
                with Vertical(classes="panel"):
                    yield Label("Stats", classes="panel-title")
                    yield Static("Attacks: 0", id="stat-attacks")
                    yield Static("Total Gold: 0", id="stat-gold")
                    yield Static("Total Elixir: 0", id="stat-elixir")
                    yield Static("Total Dark: 0", id="stat-dark")
                    yield Static("Session: 00:00:00", id="stat-session")
                    yield Static("Last Attack: —", id="stat-last")
            yield Label("Log", classes="panel-title")
            yield RichLog(id="log-view", highlight=True, markup=True)
            with Horizontal(classes="btn-row"):
                yield Button("Start", id="btn-start", variant="success")
                yield Button("Stop", id="btn-stop", variant="error", disabled=True)
                yield Button("Screenshot", id="btn-screenshot", variant="default")

    # ── Profile (In-Game Account) ────────────────────────────────

    def _compose_profile(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("— Akun CoC Terverifikasi (Auto-Sync dari Game) —", classes="section-title")
            with Horizontal(classes="form-row"):
                yield Label("Nama Akun:", classes="form-label")
                yield Input(value="Riski", id="prof-name", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Town Hall (TH):", classes="form-label")
                yield Input(placeholder="15", id="prof-th", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Gold Desa:", classes="form-label")
                yield Input(value="0", id="prof-gold-disp", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Elixir Desa:", classes="form-label")
                yield Input(value="0", id="prof-elixir-disp", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Dark Elixir Desa:", classes="form-label")
                yield Input(value="0", id="prof-dark-disp", classes="form-input")
            with Horizontal(classes="btn-row"):
                yield Button("Sync Data dari Game", id="btn-prof-sync", variant="primary")
                yield Button("Simpan Akun", id="btn-prof-save", variant="success")

    # ── Farming Rules ────────────────────────────────────────────

    def _compose_farming(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("— Rules Pencarian Target Lawan —", classes="section-title")
            with Horizontal(classes="form-row"):
                yield Label("Min Town Hall:", classes="form-label")
                yield Input(placeholder="0", id="farm-min-th", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Min Gold (default: 1):", classes="form-label")
                yield Input(placeholder="1", id="farm-min-gold", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Min Elixir (default: 1):", classes="form-label")
                yield Input(placeholder="1", id="farm-min-elixir", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Min Dark (default: 1):", classes="form-label")
                yield Input(placeholder="1", id="farm-min-dark", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Aturan Target:", classes="form-label")
                yield Select(
                    [
                        ("🎯 Salah Satu Memenuhi (Gold / Elixir / Dark)", "any"),
                        ("⚖️ Gold DAN Elixir Keduanya Memenuhi", "gold_and_elixir"),
                        ("👑 Semua Memenuhi (Gold, Elixir, & Dark)", "all"),
                    ],
                    id="farm-search-rule",
                    prompt="Pilih Aturan Target",
                    value="any",
                    classes="form-input",
                )
            with Horizontal(classes="form-row"):
                yield Label("Maksimal Next:", classes="form-label")
                yield Input(placeholder="60", id="farm-max-next", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Relaksasi Setelah:", classes="form-label")
                yield Input(placeholder="10", id="farm-relax-after", type="integer", classes="form-input")

            yield Label("— Rules Pengerahan Pasukan —", classes="section-title")
            with Horizontal(classes="form-row"):
                yield Label("Formasi Pasukan:", classes="form-label")
                yield Select(
                    [
                        ("⚔️ Formasi 1 (2 Troops + 1 Bengkel + 4 Hero + 1 Spell)", "2_troops"),
                        ("⚔️ Formasi 2 (3 Troops + 1 Bengkel + 4 Hero + 1 Spell)", "3_troops"),
                    ],
                    id="farm-army-preset",
                    prompt="Pilih Formasi Pasukan",
                    value="2_troops",
                    classes="form-input",
                )
            with Horizontal(classes="form-row"):
                yield Label("Deploy Strategy:", classes="form-label")
                yield Select(
                    [
                        ("⚡ Kerahkan Semua Kotak (All Slots)", "all_slots"),
                        ("🌊 Sebar di 2 Sisi Base", "spread_sides"),
                    ],
                    id="farm-army-deploy",
                    prompt="Pilih Strategi Deploy",
                    value="all_slots",
                    classes="form-input",
                )
                yield Label("Gunakan Heroes:", classes="form-label")
                yield Switch(id="farm-army-heroes", value=True)
            with Horizontal(classes="form-row"):
                yield Label("Gunakan Spells:", classes="form-label")
                yield Switch(id="farm-army-spells", value=True)

            with Horizontal(classes="btn-row"):
                yield Button("Simpan Rules Farming", id="btn-farm-save", variant="success")

    # ── Upgrade Rules ────────────────────────────────────────────

    def _compose_upgrade(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("— Rules Auto Upgrade —", classes="section-title")
            with Horizontal(classes="form-row"):
                yield Label("Auto Upgrade:", classes="form-label")
                yield Switch(id="upg-enabled", value=True)
            with Horizontal(classes="form-row"):
                yield Label("Fokus / Urutan:", classes="form-label")
                yield Select(
                    [
                        ("💰 Paling Murah Dulu", "cheapest"),
                        ("🌾 Resource Dulu (Gudang/Tambang)", "resource"),
                        ("🛡️ Pertahanan Dulu (Defense/Tower)", "defense"),
                        ("⚔️ Pasukan Dulu (Army/Hero/Lab)", "army"),
                        ("🧱 Tembok / Wall Dulu", "wall"),
                        ("💎 Paling Mahal Dulu", "expensive"),
                    ],
                    id="upg-mode",
                    prompt="Pilih Urutan Upgrade",
                    classes="form-input",
                )
            with Horizontal(classes="form-row"):
                yield Label("Sisa Cadangan Gold:", classes="form-label")
                yield Input(placeholder="15000", id="upg-reserve-gold", type="integer", classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Sisa Cadangan Elixir:", classes="form-label")
                yield Input(placeholder="15000", id="upg-reserve-elixir", type="integer", classes="form-input")

            with Horizontal(classes="btn-row"):
                yield Button("Simpan Rules Upgrade", id="btn-upg-save", variant="success")
    # ── Telegram ─────────────────────────────────────────────────

    def _compose_telegram(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="form-row"):
                yield Label("Enabled:", classes="form-label")
                yield Switch(id="tg-enabled", value=False)
            with Horizontal(classes="form-row"):
                yield Label("Bot Token:", classes="form-label")
                yield Input(placeholder="123456:ABC-DEF...", id="tg-token", password=True, classes="form-input")
            with Horizontal(classes="form-row"):
                yield Label("Chat ID:", classes="form-label")
                yield Input(placeholder="123456789", id="tg-chatid", classes="form-input")
            with Horizontal(classes="btn-row"):
                yield Button("Test", id="btn-tg-test", variant="primary")
                yield Button("Save", id="btn-tg-save", variant="success")
            yield Static("", id="tg-status")

    # ── Device ───────────────────────────────────────────────────

    def _compose_device(self) -> ComposeResult:
        with VerticalScroll():
            with Horizontal(classes="form-row"):
                yield Label("ADB Serial:", classes="form-label")
                yield Input(value="127.0.0.1:5555", id="dev-serial", classes="form-input")
            yield Static("Status: Disconnected", id="dev-status", classes="status-line")
            with Horizontal(classes="btn-row"):
                yield Button("Connect", id="btn-dev-connect", variant="success")
                yield Button("Disconnect", id="btn-dev-disconnect", variant="error", disabled=True)
                yield Button("Screenshot", id="btn-dev-screenshot", variant="default")

    # ── mount ────────────────────────────────────────────────────

    def on_mount(self) -> None:
        # Pipe all cocbot module logs directly to TUI log view with clear tags
        cocbot_logger = logging.getLogger("cocbot")
        cocbot_logger.setLevel(logging.INFO)
        if not any(isinstance(h, _TuiLogHandler) for h in cocbot_logger.handlers):
            cocbot_logger.addHandler(_TuiLogHandler(self))

        self._load_current_profile()
        self.set_interval(1.0, self._tick_session_timer)
        # Automatically sync with in-game COC state on startup!
        self.call_after_refresh(self._auto_sync_device)
    def _load_current_profile(self) -> None:
        try:
            from cocbot.config import default_profile, load_profile

            try:
                self._profile = load_profile()
            except Exception:
                self._profile = default_profile(name="Riski", th=4)
            self._populate_profile_fields()
        except Exception as exc:
            log.debug("Profile load error: %s", exc)

    def _populate_profile_fields(self) -> None:
        p = self._profile
        if p is None:
            return

        def _set_input(wid: str, val: Any) -> None:
            try:
                self.query_one(f"#{wid}", Input).value = str(val) if val is not None else ""
            except Exception:
                pass

        def _set_switch(wid: str, val: bool) -> None:
            try:
                self.query_one(f"#{wid}", Switch).value = bool(val)
            except Exception:
                pass

        # Profile Tab (In-Game Account)
        _set_input("prof-name", getattr(p, "name", "Riski"))
        _set_input("prof-th", getattr(p, "th", 15))
        _set_input("prof-gold-disp", _fmt_num(self._resources.get("gold", 0)))
        _set_input("prof-elixir-disp", _fmt_num(self._resources.get("elixir", 0)))
        _set_input("prof-dark-disp", _fmt_num(self._resources.get("dark", 0)))

        # Farming Tab (Farming Rules)
        search = getattr(p, "search", None)
        if search:
            _set_input("farm-min-th", getattr(search, "min_th", 0))
            _set_input("farm-min-gold", getattr(search, "min_gold", 1))
            _set_input("farm-min-elixir", getattr(search, "min_elixir", 1))
            _set_input("farm-min-dark", getattr(search, "min_dark", 1))
            try:
                self.query_one("#farm-search-rule", Select).value = getattr(search, "rule", "any")
            except Exception:
                pass
            _set_input("farm-max-next", getattr(search, "max_next", 60))
            _set_input("farm-relax-after", getattr(search, "relax_after_next", 10))

        army = getattr(p, "army", None)
        if army:
            try:
                self.query_one("#farm-army-preset", Select).value = getattr(army, "preset", "2_troops")
            except Exception:
                pass
            try:
                self.query_one("#farm-army-deploy", Select).value = getattr(army, "deploy", "all_slots")
            except Exception:
                pass
            _set_switch("farm-army-heroes", getattr(army, "use_heroes", True))
            _set_switch("farm-army-spells", getattr(army, "use_spells", True))
        # Upgrade Tab (Upgrade Rules)
        upgrade = getattr(p, "upgrade", None)
        if upgrade:
            _set_switch("upg-enabled", getattr(upgrade, "enabled", True))
            kr = getattr(upgrade, "keep_reserve", None)
            if kr:
                _set_input("upg-reserve-gold", getattr(kr, "gold", 15000) if not isinstance(kr, dict) else kr.get("gold", 15000))
                _set_input("upg-reserve-elixir", getattr(kr, "elixir", 15000) if not isinstance(kr, dict) else kr.get("elixir", 15000))

            sort_by = getattr(upgrade, "sort_by", "priority")
            priority = getattr(upgrade, "priority", [])
            if sort_by in ("wall", "cheapest", "resource", "defense", "army", "expensive"):
                mode = sort_by
            elif priority and priority[0] in ("storage", "collector"):
                mode = "resource"
            elif priority and priority[0] == "defense":
                mode = "defense"
            elif priority and priority[0] == "army":
                mode = "army"
            elif priority and priority[0] == "wall":
                mode = "wall"
            else:
                mode = "cheapest"
            try:
                self.query_one("#upg-mode", Select).value = mode
            except Exception:
                pass

        tg = getattr(p, "telegram", None)
        if tg:
            _set_switch("tg-enabled", getattr(tg, "enabled", False))
            _set_input("tg-token", getattr(tg, "bot_token", ""))
            _set_input("tg-chatid", getattr(tg, "chat_id", ""))

    def _tick_session_timer(self) -> None:
        start = self._stats.get("session_start")
        if start and self._status == "Running":
            elapsed = timedelta(seconds=int(time.time() - start))
            try:
                self.query_one("#stat-session", Static).update(f"Session: {elapsed}")
            except Exception:
                pass

    # ── button handlers ──────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-start":
            self._do_start()
        elif bid == "btn-stop":
            self._do_stop()
        elif bid in ("btn-screenshot", "btn-dev-screenshot"):
            self._do_screenshot()
        elif bid == "btn-prof-sync":
            self._auto_sync_device()
        elif bid == "btn-prof-save":
            self._do_save_profile()
        elif bid == "btn-farm-save":
            self._do_save_farming()
        elif bid == "btn-upg-save":
            self._do_save_upgrade()
        elif bid == "btn-tg-test":
            self._do_test_telegram()
        elif bid == "btn-tg-save":
            self._do_save_telegram()
        elif bid == "btn-dev-connect":
            self._do_connect()
        elif bid == "btn-dev-disconnect":
            self._do_disconnect()
    # ── actions ──────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#log-view", RichLog).write(f"[dim]{ts}[/dim] {msg}")
        except Exception:
            pass

    def _update_status(self, status: str) -> None:
        self._status = status
        try:
            color = {"Running": "green", "Stopped": "red", "Break": "yellow"}.get(
                status, "white"
            )
            self.query_one("#dash-status", Static).update(
                f"Status: [{color}]{status}[/{color}]"
            )
        except Exception:
            pass

    def _update_device_status(self, connected: bool) -> None:
        try:
            status_txt = "Status: [green]Connected[/green]" if connected else "Status: Disconnected"
            self.query_one("#dev-status", Static).update(status_txt)
            self.query_one("#btn-dev-connect", Button).disabled = connected
            self.query_one("#btn-dev-disconnect", Button).disabled = not connected
        except Exception:
            pass

    def _update_resources(
        self, gold: int | None = None, elixir: int | None = None,
        dark: int | None = None,
    ) -> None:
        mapping = {"gold": gold, "elixir": elixir, "dark": dark}
        for key, val in mapping.items():
            if val is not None:
                self._resources[key] = val
            try:
                self.query_one(f"#res-{key}", Static).update(
                    f"{key.title()}: {_fmt_num(self._resources[key])}"
                )
            except Exception:
                pass

    def _update_stats(self) -> None:
        try:
            self.query_one("#stat-attacks", Static).update(f"Attacks: {self._stats['attacks']}")
            self.query_one("#stat-gold", Static).update(f"Total Gold: {_fmt_num(self._stats['gold'])}")
            self.query_one("#stat-elixir", Static).update(f"Total Elixir: {_fmt_num(self._stats['elixir'])}")
            self.query_one("#stat-dark", Static).update(f"Total Dark: {_fmt_num(self._stats['dark'])}")
            la = self._stats.get("last_attack")
            la_str = la.strftime("%H:%M:%S") if la else "—"
            self.query_one("#stat-last", Static).update(f"Last Attack: {la_str}")
        except Exception:
            pass

    # ── bot event callback (called from runner thread) ───────────

    def bot_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Callback for BotRunner to push events into the TUI."""
        data = data or {}
        self.call_from_thread(self._handle_bot_event, event_type, data)

    def _handle_bot_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "log":
            self._log(data.get("message", ""))
        elif event_type == "started":
            self._update_status("Running")
            self._log("[green]Bot started[/green]")
        elif event_type == "stopped":
            self._update_status("Stopped")
            reason = data.get("reason", "")
            self._log(f"Bot stopped{f' ({reason})' if reason else ''}")
        elif event_type == "error":
            self._log(f"[red]Error: {data.get('reason', 'unknown')}[/red]")
        elif event_type == "attack_complete":
            self._stats["attacks"] += 1
            self._stats["gold"] += data.get("loot_gold", 0)
            self._stats["elixir"] += data.get("loot_elixir", 0)
            self._stats["dark"] += data.get("loot_dark", 0)
            self._stats["last_attack"] = datetime.now()
            self._update_stats()
            self._log(
                f"Attack #{data.get('attack_number', '?')}: "
                f"G={_fmt_num(data.get('loot_gold'))} "
                f"E={_fmt_num(data.get('loot_elixir'))} "
                f"D={_fmt_num(data.get('loot_dark'))}"
            )
        elif event_type == "upgrade_started":
            self._log("[cyan]Builder upgrade started[/cyan]")
        elif event_type == "resources":
            self._update_resources(
                data.get("gold"),
                data.get("elixir"),
                data.get("dark"),
            )
        elif event_type == "break_started":
            mins = data.get("duration_minutes", "?")
            self._update_status("Break")
            self._log(f"[yellow]Break: {mins} min[/yellow]")
        elif event_type == "break_ended":
            self._update_status("Running")
            self._log("Break over — resuming")

    # ── Start / Stop ─────────────────────────────────────────────

    @work(thread=True)
    def _do_start(self) -> None:
        if self._status == "Running":
            return

        self.call_from_thread(self._log, "Starting bot...")

        # Ensure ADB connected
        if self._adb is None:
            self.call_from_thread(self._log, "Connecting ADB...")
            self._ensure_adb()
            if self._adb is None:
                self.call_from_thread(self._log, "[red]ADB connection failed[/red]")
                return

        # Load profile
        if self._profile is None:
            self._load_current_profile()
            if self._profile is None:
                self.call_from_thread(self._log, "[red]No profile loaded[/red]")
                return

        # Create Vision
        try:
            from cocbot.vision import Vision
            self._vision = Vision()
        except Exception as exc:
            self.call_from_thread(self._log, f"[red]Vision init failed: {exc}[/red]")
            return

        # Load the latest profile saved by user
        try:
            from cocbot.config import load_profile
            self._profile = load_profile()
            self.call_from_thread(
                self._log,
                f"[cyan]Loaded profile '{self._profile.name}' (TH{self._profile.th}, upgrade: {self._profile.upgrade.sort_by})[/cyan]"
            )
        except Exception as exc:
            self.call_from_thread(self._log, f"[yellow]Profile reload notice: {exc}[/yellow]")

        try:
            from cocbot.game.runner import BotRunner
            self._runner = BotRunner(
                device=self._adb,
                vision=self._vision,
                config=self._profile,
                on_event=self.bot_event,
            )
        except Exception as exc:
            self.call_from_thread(self._log, f"[red]Runner init failed: {exc}[/red]")
            return

        # Start telegram if configured
        self._start_telegram()

        self._stats["session_start"] = time.time()
        self.call_from_thread(self._update_status, "Running")
        self.call_from_thread(
            lambda: (
                self.query_one("#btn-start", Button).__setattr__("disabled", True),
                self.query_one("#btn-stop", Button).__setattr__("disabled", False),
            )
        )
        self.call_from_thread(self._log, "[green]Bot started[/green]")

        try:
            self._runner.run()
        except Exception as exc:
            self.call_from_thread(self._log, f"[red]Runner error: {exc}[/red]")
        finally:
            self.call_from_thread(self._update_status, "Stopped")
            self.call_from_thread(
                lambda: (
                    self.query_one("#btn-start", Button).__setattr__("disabled", False),
                    self.query_one("#btn-stop", Button).__setattr__("disabled", True),
                )
            )
            self.call_from_thread(self._log, "Bot stopped")

    def _do_stop(self) -> None:
        if self._runner is not None:
            self._runner.stop()
            self._log("Stopping bot...")
        if self._tg is not None:
            self._tg.stop_polling()

    # ── ADB ──────────────────────────────────────────────────────

    def _ensure_adb(self) -> None:
        try:
            from cocbot.adb import AdbDevice

            serial = "127.0.0.1:5555"
            try:
                serial = self.query_one("#dev-serial", Input).value or serial
            except Exception:
                pass
            self._adb = AdbDevice(serial)
            self._adb.connect()
        except Exception as exc:
            log.exception("ADB connect error")
            self._adb = None

    @work(thread=True)
    def _do_connect(self) -> None:
        self.call_from_thread(self._log, "Connecting to ADB...")
        self._ensure_adb()
        if self._adb is not None and self._adb.is_connected():
            self.call_from_thread(
                lambda: self.query_one("#dev-status", Static).update(
                    "Status: [green]Connected[/green]"
                )
            )
            self.call_from_thread(
                lambda: (
                    self.query_one("#btn-dev-connect", Button).__setattr__("disabled", True),
                    self.query_one("#btn-dev-disconnect", Button).__setattr__("disabled", False),
                )
            )
            self.call_from_thread(self._log, "[green]ADB connected[/green]")
        else:
            self.call_from_thread(
                lambda: self.query_one("#dev-status", Static).update(
                    "Status: [red]Connection Failed[/red]"
                )
            )
            self.call_from_thread(self._log, "[red]ADB connection failed[/red]")

    def _do_disconnect(self) -> None:
        self._adb = None
        self.query_one("#dev-status", Static).update("Status: Disconnected")
        self.query_one("#btn-dev-connect", Button).disabled = False
        self.query_one("#btn-dev-disconnect", Button).disabled = True
        self._log("ADB disconnected")

    @work(thread=True)
    def _do_screenshot(self) -> None:
        if self._adb is None:
            self.call_from_thread(self._log, "[red]ADB not connected[/red]")
            return
        try:
            import cv2

            from cocbot.paths import data_dir

            img = self._adb.screenshot()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = data_dir() / "screenshots" / f"{ts}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), img)
            self.call_from_thread(self._log, f"Screenshot saved: {path}")
        except Exception as exc:
            self.call_from_thread(self._log, f"[red]Screenshot failed: {exc}[/red]")

    # ── Profile actions ──────────────────────────────────────────

    def _do_save_profile(self) -> None:
        try:
            from cocbot.config import save_profile
            if self._profile is None:
                return
            name = self.query_one("#prof-name", Input).value.strip() or self._profile.name
            try:
                th = int(self.query_one("#prof-th", Input).value)
            except Exception:
                th = self._profile.th
            self._profile.name = name
            self._profile.th = th
            save_profile(self._profile)
            self._log(f"[green]Data akun '{name}' (TH{th}) berhasil disimpan![/green]")
        except Exception as exc:
            self._log(f"[red]Gagal simpan akun: {exc}[/red]")

    def _do_save_farming(self) -> None:
        try:
            from cocbot.config import save_profile
            if self._profile is None:
                return

            def _get_int(wid: str, default: int = 1) -> int:
                try:
                    return int(self.query_one(f"#{wid}", Input).value)
                except Exception:
                    return default

            def _get_str(wid: str, default: str = "") -> str:
                try:
                    return str(self.query_one(f"#{wid}", Input).value).strip() or default
                except Exception:
                    return default

            def _get_switch(wid: str, default: bool = True) -> bool:
                try:
                    return bool(self.query_one(f"#{wid}", Switch).value)
                except Exception:
                    return default

            # Update search config
            self._profile.search.min_th = _get_int("farm-min-th", 0)
            self._profile.search.min_gold = _get_int("farm-min-gold", 1)
            self._profile.search.min_elixir = _get_int("farm-min-elixir", 1)
            self._profile.search.min_dark = _get_int("farm-min-dark", 1)
            def _get_select(wid: str, default: str = "") -> str:
                try:
                    sel = self.query_one(f"#{wid}", Select)
                    if sel.value != Select.BLANK and sel.value is not None:
                        return str(sel.value)
                    return default
                except Exception:
                    return default

            self._profile.search.rule = _get_select("farm-search-rule", "any")
            self._profile.search.max_next = _get_int("farm-max-next", 60)
            self._profile.search.relax_after_next = _get_int("farm-relax-after", 10)

            # Update army config
            preset = _get_select("farm-army-preset", "2_troops")
            self._profile.army.preset = preset
            self._profile.army.slots = 8 if preset == "2_troops" else 9
            self._profile.army.deploy = _get_select("farm-army-deploy", "all_slots")
            self._profile.army.use_heroes = _get_switch("farm-army-heroes", True)
            self._profile.army.use_spells = _get_switch("farm-army-spells", True)
            save_profile(self._profile)
            self._log(
                f"[green]Rules Farming disimpan: Target Min G={self._profile.search.min_gold}, "
                f"E={self._profile.search.min_elixir}, D={self._profile.search.min_dark} | Max Next: {self._profile.search.max_next}[/green]"
            )
        except Exception as exc:
            self._log(f"[red]Gagal simpan rules farming: {exc}[/red]")

    def _do_save_upgrade(self) -> None:
        try:
            from cocbot.config import save_profile
            if self._profile is None:
                return

            def _get_int(wid: str, default: int = 15000) -> int:
                try:
                    return int(self.query_one(f"#{wid}", Input).value)
                except Exception:
                    return default

            def _get_switch(wid: str, default: bool = True) -> bool:
                try:
                    return bool(self.query_one(f"#{wid}", Switch).value)
                except Exception:
                    return default

            mode = "cheapest"
            try:
                sel = self.query_one("#upg-mode", Select)
                if sel.value != Select.BLANK and sel.value is not None:
                    mode = str(sel.value)
            except Exception:
                pass

            if mode == "defense":
                priority = ["defense", "wall", "army", "storage", "collector", "hero"]
            elif mode == "army":
                priority = ["army", "hero", "defense", "storage", "collector", "wall"]
            elif mode == "wall":
                priority = ["wall", "storage", "collector", "defense", "army", "hero"]
            else:
                priority = ["storage", "collector", "army", "defense", "hero", "wall"]

            self._profile.upgrade.enabled = _get_switch("upg-enabled", True)
            self._profile.upgrade.sort_by = mode
            self._profile.upgrade.priority = priority
            self._profile.upgrade.keep_reserve = {
                "gold": _get_int("upg-reserve-gold", 15000),
                "elixir": _get_int("upg-reserve-elixir", 15000),
            }

            save_profile(self._profile)
            self._log(f"[green]Rules Upgrade disimpan! Fokus: {mode}[/green]")
        except Exception as exc:
            self._log(f"[red]Gagal simpan rules upgrade: {exc}[/red]")

    @work(thread=True)
    def _auto_sync_device(self) -> None:
        """Connect to BlueStacks on launch, read COC game state, and populate profile fields."""
        try:
            from cocbot.adb import AdbDevice
            from cocbot.vision import Vision
            from cocbot.config import default_profile, load_profile, save_profile

            self.call_from_thread(self._log, "[cyan]Connecting to BlueStacks and syncing COC account...[/cyan]")
            adb = AdbDevice()
            if not adb.connect():
                self.call_from_thread(
                    self._log,
                    "[yellow]BlueStacks not detected yet. Start BlueStacks & Clash of Clans, then click Connect in Device tab.[/yellow]"
                )
                return

            self._adb = adb
            self._vision = Vision()
            self.call_from_thread(self._update_device_status, True)

            img = adb.screenshot()
            if img is not None:
                info = self._vision.auto_detect_profile(img, device=adb)
                detected_name = info.get("name") or "Riski"
                detected_th = info.get("th") or 4

                # Update resources on Dashboard
                self.call_from_thread(
                    self._update_resources,
                    info.get("gold"),
                    info.get("elixir"),
                    info.get("dark"),
                )

                try:
                    self._profile = load_profile()
                except Exception:
                    self._profile = default_profile(name=detected_name, th=detected_th)

                if detected_name and detected_name.lower() != "player":
                    self._profile.name = detected_name
                if detected_th > 0:
                    self._profile.th = detected_th

                save_profile(self._profile)
                self.call_from_thread(self._populate_profile_fields)
                self.call_from_thread(
                    self._log,
                    f"[green]Account auto-synced: {self._profile.name} (TH{self._profile.th}) | "
                    f"Gold: {info.get('gold', 0):,} | Elixir: {info.get('elixir', 0):,} | "
                    f"Ready! Configure settings then click Start.[/green]"
                )
        except Exception as exc:
            self.call_from_thread(self._log, f"[yellow]Auto-sync notice: {exc}[/yellow]")
    # ── Telegram actions ─────────────────────────────────────────

    def _start_telegram(self) -> None:
        if self._profile is None:
            return
        tg_cfg = getattr(self._profile, "telegram", None)
        if tg_cfg is None:
            return
        enabled = getattr(tg_cfg, "enabled", False)
        if not enabled:
            return
        token = getattr(tg_cfg, "bot_token", "")
        chat_id = getattr(tg_cfg, "chat_id", "")
        if not token or not chat_id:
            return
        from cocbot.telegram_bot import TelegramBot

        self._tg = TelegramBot(token, chat_id)
        self._tg.send("🤖 COCBOT started")
        self._tg.start_polling(self._handle_tg_command)

    def _handle_tg_command(self, cmd: str) -> str:
        cmd = cmd.strip().lower()
        if cmd == "/status":
            return (
                f"Status: {self._status}\n"
                f"Attacks: {self._stats['attacks']}\n"
                f"Gold: {_fmt_num(self._stats['gold'])}\n"
                f"Elixir: {_fmt_num(self._stats['elixir'])}\n"
                f"Dark: {_fmt_num(self._stats['dark'])}"
            )
        elif cmd == "/stop":
            self._do_stop()
            return "⏹ Bot stopping..."
        elif cmd == "/start":
            return "Use the TUI Start button to start the bot."
        elif cmd == "/screenshot":
            if self._adb is not None and self._tg is not None:
                try:
                    import cv2

                    img = self._adb.screenshot()
                    _, buf = cv2.imencode(".png", img)
                    self._tg.send_photo(buf.tobytes(), caption="Screenshot")
                    return "📸 Screenshot sent"
                except Exception as exc:
                    return f"Screenshot failed: {exc}"
            return "ADB not connected"
        else:
            return "Unknown command. Try /status /stop /screenshot"

    @work(thread=True)
    def _do_test_telegram(self) -> None:
        token = self.query_one("#tg-token", Input).value
        chat_id = self.query_one("#tg-chatid", Input).value
        if not token or not chat_id:
            self.call_from_thread(
                lambda: self.query_one("#tg-status", Static).update(
                    "[red]Token and Chat ID required[/red]"
                )
            )
            return
        try:
            from cocbot.telegram_bot import TelegramBot

            bot = TelegramBot(token, chat_id)
            bot.send("✅ COCBOT test message — connection OK!")
            self.call_from_thread(
                lambda: self.query_one("#tg-status", Static).update(
                    "[green]Test message sent![/green]"
                )
            )
        except Exception as exc:
            self.call_from_thread(
                lambda: self.query_one("#tg-status", Static).update(
                    f"[red]Failed: {exc}[/red]"
                )
            )

    def _do_save_telegram(self) -> None:
        # Save telegram config into the current profile
        if self._profile is None:
            self._log("[red]No profile loaded[/red]")
            return
        try:
            from cocbot.config import save_profile

            tg_cfg = getattr(self._profile, "telegram", None)
            enabled = self.query_one("#tg-enabled", Switch).value
            token = self.query_one("#tg-token", Input).value
            chat_id = self.query_one("#tg-chatid", Input).value

            # Update profile telegram attrs directly
            if tg_cfg is not None:
                tg_cfg.enabled = enabled
                tg_cfg.bot_token = token
                tg_cfg.chat_id = chat_id
            else:
                # Try setting as dict if profile allows
                self._profile.telegram = type("TgCfg", (), {
                    "enabled": enabled, "bot_token": token, "chat_id": chat_id
                })()

            save_profile(self._profile_name, self._profile)
            self._log("[green]Telegram config saved[/green]")
        except Exception as exc:
            self._log(f"[red]Save telegram failed: {exc}[/red]")
