# COCBOT — Clash of Clans Farming Bot

Automated farming bot for Clash of Clans running on BlueStacks 5 via ADB.

## Requirements

- **Windows 10/11** (uses WinRT OCR)
- **BlueStacks 5** — Settings → Display: **1280×720, DPI 240**. Settings → Advanced: **ADB enabled**
- **Python 3.12+**
- **ADB** (from Android SDK platform-tools, or bundled with BlueStacks)

## Install

```bash
pip install opencv-python numpy pyyaml pydantic textual httpx
pip install winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Globalization
```

## Quick Start

1. Start BlueStacks and open Clash of Clans. Login manually.
2. Run the bot:

```bash
# TUI mode (terminal dashboard)
python -m cocbot

# Headless mode (no UI, just logs)
python -m cocbot --headless --profile default
```

## Configuration

Profiles are stored as YAML in `data/profiles/`. Edit via TUI (Profile tab) or directly:

```yaml
# data/profiles/default.yaml
name: default
th: 9
mode: classic

stop_when:
  rule: any          # any | all
  gold: full         # "full" or integer (e.g. 5000000)
  elixir: full
  dark: 0

search:
  min_gold: 200000
  min_elixir: 200000
  min_dark: 0
  rule: gold_and_elixir   # gold_and_elixir | any | all
  max_next: 150
  relax_after_next: 40
  relax_percent: 10

army:
  source: current         # uses whatever army is trained in-game
  deploy: spread_sides
  sides: 2
  use_heroes: true
  use_spells: false

upgrade:
  enabled: true
  keep_reserve:
    gold: 100000
    elixir: 100000
  priority:
    - storage
    - collector
    - army
    - defense
    - hero
    - wall
  walls_when_resource_over: 0.9

humanize:
  session_minutes: [60, 150]
  break_minutes: [15, 45]
  tap_jitter_px: 6
  delay_ms: [120, 450]

telegram:
  enabled: false
  bot_token: ""
  chat_id: ""
```

## Telegram Bot

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token.
2. Send any message to your bot, then get your chat ID via `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Set `telegram.enabled: true`, paste `bot_token` and `chat_id` in the profile (or via TUI Telegram tab).

Commands your bot responds to:
- `/status` — current bot stats
- `/stop` — stop the bot
- `/start` — resume the bot
- `/screenshot` — capture and send current screen

## Architecture

```
cocbot/
  __main__.py       — CLI entrypoint
  config.py         — Pydantic models + YAML persistence
  adb.py            — ADB device: screencap, fast tap (dd to /dev/input/event4), swipe, pinch
  ocr.py            — WinRT OCR wrapper
  vision.py         — Screen detection, resource/loot/builder reading
  telegram_bot.py   — Telegram notifications via httpx
  paths.py          — Data directory helpers
  game/
    navigator.py    — Screen transitions, popup dismissal
    attack.py       — Search loop, loot filter, troop deployment
    builder.py      — Upgrade selection and confirmation
    runner.py       — Main bot loop with session/break cycle
  tui/
    app.py          — Textual TUI (Dashboard, Profile, Telegram, Device tabs)
```

## How It Works

1. Bot ensures it's on the Home screen (dismisses popups/modals)
2. Reads current resources via OCR
3. Checks stop conditions (gold/elixir/dark targets)
4. If builder is free and resources sufficient → starts an upgrade
5. Opens attack menu → Classic mode → searches for targets
6. Filters targets by minimum loot thresholds (relaxes after many skips)
7. Deploys troops along 1–4 sides of the enemy base
8. Waits for battle to end, returns home
9. After configurable session time → takes a random-length break
10. Repeats until stop conditions met or manually stopped

## Notes

- **Account safety**: This violates Supercell ToS. Use on disposable accounts only.
- **Resolution**: Locked to 1280×720. Changing resolution breaks all coordinate-based logic.
- **Language**: Game must be in Indonesian (the OCR markers are tuned for it).
- **Account switching**: Manual only. Bot farms whatever account is currently logged in.
- **ADB serial**: Default `127.0.0.1:5555`. Change via TUI Device tab or environment variable.
