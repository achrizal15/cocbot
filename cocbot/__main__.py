"""CLI entrypoint: ``python -m cocbot``."""

from __future__ import annotations

import argparse
import logging
import sys


def _resolve_profile(
    adb: AdbDevice, vision: Vision, requested: str | None = None
) -> Any:
    """Auto-detect player name and TH from in-game screen and load/create profile."""
    from cocbot.config import default_profile, load_profile, save_profile

    log = logging.getLogger("cocbot")
    img = adb.screenshot()
    info: dict = {}
    if img is not None:
        info = vision.auto_detect_profile(img, device=adb)

    detected_name = info.get("name")
    detected_th = info.get("th") or 4

    # If user provided a specific profile name, respect it
    if requested and requested != "default":
        profile_name = requested
    else:
        profile_name = "active"
    try:
        profile = load_profile(profile_name)
        log.info("Loaded profile: %s (TH%d)", profile.name, profile.th)
    except FileNotFoundError:
        profile = default_profile(name=profile_name, th=detected_th)
        save_profile(profile)
        log.info(
            "Auto-created profile '%s' (TH%d) from in-game account",
            profile_name,
            detected_th,
        )

    log.info(
        "Account auto-detected: Player=%s, TH%d, Gold=%s, Elixir=%s, Dark=%s, Builder=%d/%d",
        info.get("name", profile_name),
        profile.th,
        f'{info.get("gold", 0):,}',
        f'{info.get("elixir", 0):,}',
        f'{info.get("dark", 0):,}',
        info.get("builder_free", 0),
        info.get("builder_total", 0),
    )
    return profile


def _run_headless(requested_profile: str | None = None) -> None:
    """Run the bot loop without a TUI."""
    from cocbot.adb import AdbDevice
    from cocbot.game.runner import BotRunner
    from cocbot.telegram_bot import TelegramBot
    from cocbot.vision import Vision

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("cocbot")

    adb = AdbDevice()
    if not adb.connect():
        log.error("ADB connection failed — check that BlueStacks is running")
        sys.exit(1)
    log.info("ADB connected")

    vision = Vision()
    profile = _resolve_profile(adb, vision, requested=requested_profile)
    def _callback(event_type: str, data: dict | None = None) -> None:
        data = data or {}
        if event_type == "attack_complete":
            log.info(
                "Attack #%s: G=%s E=%s D=%s",
                data.get("attack_number", "?"),
                f'{data.get("loot_gold", 0):,}',
                f'{data.get("loot_elixir", 0):,}',
                f'{data.get("loot_dark", 0):,}',
            )
        elif event_type == "break_started":
            log.info("Break: %.1f min", data.get("duration_minutes", 0))
        elif event_type == "break_ended":
            log.info("Break over — resuming")
        elif event_type == "upgrade_started":
            log.info("Builder upgrade started")
        elif event_type == "stopped":
            reason = data.get("reason", "")
            log.info("Stopped%s", f" ({reason})" if reason else "")

    runner = BotRunner(device=adb, vision=vision, config=profile, on_event=_callback)

    # Start telegram if configured
    tg = None
    tg_cfg = getattr(profile, "telegram", None)
    if tg_cfg and getattr(tg_cfg, "enabled", False):
        token = getattr(tg_cfg, "bot_token", "")
        chat_id = getattr(tg_cfg, "chat_id", "")
        if token and chat_id:
            tg = TelegramBot(token, chat_id)
            tg.send("🤖 COCBOT started (headless)")

            def _handle_cmd(cmd: str) -> str:
                cmd = cmd.strip().lower()
                if cmd == "/status":
                    return f"Status: Running (headless)\nProfile: {profile.name}"
                elif cmd == "/stop":
                    runner.stop()
                    return "⏹ Bot stopping..."
                else:
                    return "Headless mode. Commands: /status /stop"

            tg.start_polling(_handle_cmd)
            log.info("Telegram bot started")

    try:
        runner.run()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        runner.stop()
    finally:
        if tg is not None:
            tg.send("🤖 COCBOT stopped")
            tg.stop_polling()
        log.info("Bot stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cocbot",
        description="COCBOT — Clash of Clans Farming Bot",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without TUI (headless bot loop)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Profile name (default: auto-detected from in-game player name)",
    )
    args = parser.parse_args()

    if args.headless:
        _run_headless(args.profile)
    else:
        from cocbot.tui.app import CocBotApp

        app = CocBotApp()
        app.run()

if __name__ == "__main__":
    main()
