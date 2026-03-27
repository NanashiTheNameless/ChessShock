"""Command line entry point for ChessShock."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Tuple

from .config import (
    ConfigError,
    build_default_config,
    default_config_path,
    load_config,
)
from .lichess import LichessClient, LichessError, LichessRateLimitError
from .monitor import ChessShockMonitor
from .oauth import LichessOAuthError
from .setup_wizard import refresh_lichess_oauth_token, run_configuration_wizard


def build_parser() -> argparse.ArgumentParser:
    default_path = str(default_config_path())
    parser = argparse.ArgumentParser(
        prog="ChessShock",
        description="Poll Lichess games and optionally trigger OpenShock.",
    )
    parser.add_argument(
        "--config",
        default=default_path,
        help="Path to the JSON config file (default: {0}).".format(default_path),
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Run the interactive setup wizard and write the config file.",
    )
    parser.add_argument(
        "--oauth-login",
        action="store_true",
        help="Run the Lichess OAuth login flow and update the stored token.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll and exit.",
    )
    parser.add_argument(
        "--list-shockers",
        action="store_true",
        help="List OpenShock shockers from the configured account and exit.",
    )
    return parser


def main(argv: List[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = str(Path(args.config).expanduser())

    if args.configure:
        try:
            existing = _maybe_load_existing_config(args.config)
            run_configuration_wizard(args.config, existing_config=existing)
            return 0
        except (ConfigError, LichessOAuthError) as exc:
            print("Setup failed: {0}".format(exc), file=sys.stderr)
            return 2

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        if not _is_interactive_terminal():
            print("Config error: {0}".format(exc), file=sys.stderr)
            return 2

        print("Config error: {0}".format(exc), file=sys.stderr)
        print(
            "Starting interactive configuration because this terminal is interactive.",
            file=sys.stderr,
        )
        try:
            existing = _maybe_load_existing_config(args.config)
            config = run_configuration_wizard(args.config, existing_config=existing)
        except (ConfigError, LichessOAuthError) as setup_exc:
            print("Setup failed: {0}".format(setup_exc), file=sys.stderr)
            return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.oauth_login:
        try:
            config = refresh_lichess_oauth_token(config, args.config)
        except (ConfigError, LichessOAuthError) as exc:
            print("OAuth login failed: {0}".format(exc), file=sys.stderr)
            return 1

    if not config.lichess.api_token:
        if not config.lichess.oauth.enabled:
            print(
                "Config error: no Lichess token is available and lichess.oauth.enabled is false",
                file=sys.stderr,
            )
            return 2
        if not _is_interactive_terminal():
            print(
                "Config error: this config requires interactive OAuth login, but the terminal is not interactive",
                file=sys.stderr,
            )
            return 2
        try:
            config = refresh_lichess_oauth_token(config, args.config)
        except (ConfigError, LichessOAuthError) as exc:
            print("OAuth login failed: {0}".format(exc), file=sys.stderr)
            return 1

    try:
        from OpenShockPY import OpenShockClient, OpenShockPYError
    except ImportError:
        print(
            "Dependency error: install the package and GitHub-backed dependencies with `python -m pip install -r requirements.txt`",
            file=sys.stderr,
        )
        return 1

    handled_errors = (
        LichessError,
        LichessOAuthError,
        OpenShockPYError,
        ConfigError,
        OSError,
    )
    lichess_client = LichessClient(
        api_token=config.lichess.api_token,
        user_agent=config.user_agent,
        base_url=config.lichess.api_base_url,
    )
    openshock_client = None

    try:
        if args.list_shockers or config.openshock.enabled:
            api_token = config.openshock.resolved_api_token()
            if not api_token:
                raise ConfigError(
                    "An OpenShock API token is required for the requested operation"
                )
            openshock_client = OpenShockClient(
                api_key=api_token,
                user_agent=config.user_agent,
            )

        if args.list_shockers:
            _print_shockers(openshock_client.list_shockers())
            return 0

        monitor = ChessShockMonitor(
            config=config,
            lichess_client=lichess_client,
            openshock_client=openshock_client,
        )

        if args.once:
            monitor.poll_once()
            return 0

        if config.poll_interval_seconds < 2:
            logging.getLogger("chessshock").warning(
                "poll_interval_seconds is below 2s; the Lichess API is rate limited, so keep requests sequential and conservative"
            )
        logging.getLogger("chessshock").info(
            "Watching Lichess every %ss", config.poll_interval_seconds
        )

        while True:
            delay_seconds = _run_poll_cycle(
                monitor,
                handled_errors,
                default_poll_interval=config.poll_interval_seconds,
            )
            time.sleep(delay_seconds)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except handled_errors as exc:
        print("Error: {0}".format(exc), file=sys.stderr)
        return 1
    finally:
        if openshock_client is not None:
            openshock_client.close()


def _run_poll_cycle(
    monitor: ChessShockMonitor,
    handled_errors,
    *,
    default_poll_interval: int,
) -> int:
    try:
        monitor.poll_once()
        return default_poll_interval
    except LichessRateLimitError as exc:
        delay_seconds = max(default_poll_interval, exc.retry_after_seconds)
        logging.getLogger("chessshock").error(
            "Polling hit the Lichess rate limit; backing off for %ss",
            delay_seconds,
        )
        return delay_seconds
    except handled_errors as exc:
        logging.getLogger("chessshock").error("Polling failed: %s", exc)
        return default_poll_interval


def _maybe_load_existing_config(path: str):
    try:
        return load_config(path)
    except ConfigError:
        defaults = build_default_config()
        defaults.username = "your_lichess_username"
        return defaults


def _is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_shockers(response: dict) -> None:
    rows = _flatten_shockers(response)
    if not rows:
        print("No shockers found")
        return

    for device_name, shocker_name, shocker_id in rows:
        print(
            "device={0} shocker={1} id={2}".format(
                device_name,
                shocker_name,
                shocker_id,
            )
        )


def _flatten_shockers(response: dict) -> List[Tuple[str, str, str]]:
    rows = []
    for entry in response.get("data", []):
        if not isinstance(entry, dict):
            continue

        if isinstance(entry.get("shockers"), list):
            device_name = str(entry.get("name", "<unnamed device>"))
            for shocker in entry["shockers"]:
                if not isinstance(shocker, dict):
                    continue
                rows.append(
                    (
                        device_name,
                        str(shocker.get("name", "<unnamed shocker>")),
                        str(shocker.get("id", "")),
                    )
                )
        else:
            rows.append(
                (
                    "<unknown device>",
                    str(entry.get("name", "<unnamed shocker>")),
                    str(entry.get("id", "")),
                )
            )
    return rows
