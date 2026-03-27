# ChessShock

`ChessShock` monitors your Lichess account and can trigger OpenShock actions when:

- it becomes your turn in an ongoing game
- a newly finished game is detected as a loss

Python 3.10 or newer is required.

## Install

Recommended with `pipx`:

```bash
pipx install --force 'git+https://github.com/NanashiTheNameless/ChessShock@main'
```

If you do not already have `pipx`, install it first and make sure its bin directory is on your `PATH`.

Local checkout fallback:

```bash
python -m pip install -r requirements.txt
```

Both install paths need network access and `git`, because `OpenShockPY` is installed from GitHub.

After installation, the console command is:

```bash
ChessShock
```

## Config Location

By default, ChessShock reads and writes:

- Linux: `~/.config/ChessShock/config.json`
- macOS: `~/Library/Application Support/ChessShock/config.json`
- Windows: `%APPDATA%\ChessShock\config.json`

You can override that with `--config /path/to/config.json`.

## Quick Start

Fastest path:

```bash
ChessShock --configure
```

The setup wizard will:

- ask for your contact email and build the `User-Agent` for you
- log you into Lichess with OAuth
- detect your Lichess username automatically
- ask for your OpenShock API token every time you run the wizard
- prompt for turn and loss alert settings
- write the config file to your default config directory

If you prefer editing by hand:

```bash
mkdir -p ~/.config/ChessShock
cp config.example.json ~/.config/ChessShock/config.json
```

Then update the placeholders in [config.example.json](/home/null/ChessShock/config.example.json).

## Authentication

### Lichess

ChessShock supports:

- OAuth login through the wizard
- a manually pasted personal access token in `lichess.api_token`

If `lichess.api_token` is empty and `lichess.oauth.enabled` is `true`, ChessShock can refresh the token interactively later with:

```bash
ChessShock --oauth-login
```

### OpenShock

ChessShock uses an OpenShock API token stored in:

```json
"openshock": {
  "api_token": "..."
}
```

During setup, ChessShock opens:

- `https://next.openshock.app/settings/api-tokens`

if you do not already have a saved token, so you can create one and paste it into the wizard.

## Config Overview

The main config sections are:

- `username`: your Lichess username
- `user_agent`: must include a real contact email address
- `poll_interval_seconds`: how often ChessShock polls Lichess
- `lichess`: API base URL, token, and OAuth settings
- `openshock`: whether OpenShock is enabled, your API token, and default `shocker_id`
- `alerts.turn`: settings for turn alerts
- `alerts.loss`: settings for loss alerts

Current default alert behavior from [config.example.json](/home/null/ChessShock/config.example.json):

- turn: enabled, `vibrate`, 600 ms, intensity 100, 5 second cooldown
- loss: enabled, `shock`, 800 ms, intensity 45, 5 second cooldown

Important config notes:

- `openshock.enabled` can stay `false` while you test
- `openshock.shocker_id` defaults to `"all"`
- each alert can optionally override `shocker_id`
- placeholder emails like `you@example.com` are rejected
- legacy `openshock.api_key` is still accepted on load, but `api_token` is the canonical field now

## Commands

Run continuously with the default config path:

```bash
ChessShock
```

Run one poll and exit:

```bash
ChessShock --once
```

List available OpenShock shockers:

```bash
ChessShock --list-shockers
```

Use a custom config path:

```bash
ChessShock --config /path/to/config.json
```

`python -m chessshock` also works if you prefer module execution.

## How It Works

ChessShock uses the official Lichess API:

- `GET /api/account` for token verification and username discovery
- `GET /api/account/playing` for ongoing games
- `GET /oauth` and `POST /api/token` for Lichess OAuth login with PKCE
- `GET /api/games/user/{username}` for recently finished games

Behavior details:

- Turn alerts are based on the authenticated ongoing-games feed.
- Loss alerts are based on recently finished games from the user export feed.
- On startup, ChessShock can still catch a very recent loss if it happened shortly before the app launched.
- If Lichess returns `429`, ChessShock backs off instead of continuing at the normal poll interval.

## Limitations

- Turn alerts are best-effort, not frame-perfect.
- Finished-game exports can appear slightly after the game actually ends.
- ChessShock polls sequentially and should be kept conservative to respect Lichess rate limits.
- The app currently supports turn and loss alerts only.

## Safety

- Keep `openshock.enabled` off until your config is correct.
- Double-check intensity and duration values before enabling real actions.
- Read OpenShock’s safety guidance before using hardware: `https://wiki.openshock.org/home/safety-rules`
