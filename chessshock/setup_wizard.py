"""Interactive setup helpers for ChessShock."""

from __future__ import annotations

import getpass
import sys
import webbrowser
from pathlib import Path
from typing import Callable

from .config import (
    AppConfig,
    DEFAULT_CONTACT_EMAIL,
    DEFAULT_OPENSHOCK_TOKEN_PAGE_URL,
    ConfigError,
    build_user_agent,
    LichessConfig,
    LichessOAuthConfig,
    build_default_config,
    extract_contact_email,
    save_config,
    validate_contact_email,
)
from .lichess import LichessClient, LichessError
from .oauth import obtain_oauth_token

InputFn = Callable[[str], str]
SecretInputFn = Callable[[str], str]
PrintFn = Callable[..., None]


def _masked_secret_input(prompt: str) -> str:
    """Read a secret from the terminal while echoing mask characters."""
    if not _supports_masked_terminal_input():
        return getpass.getpass(prompt)

    try:
        if sys.platform == "win32":
            return _read_masked_secret_windows(prompt)
        return _read_masked_secret_posix(prompt)
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        return getpass.getpass(prompt)


def _supports_masked_terminal_input() -> bool:
    stdin = sys.stdin
    stdout = sys.stdout
    return (
        stdin is not None
        and stdout is not None
        and hasattr(stdin, "isatty")
        and hasattr(stdout, "isatty")
        and stdin.isatty()
        and stdout.isatty()
    )


def _read_masked_secret(
    prompt: str,
    *,
    read_char_fn: Callable[[], str],
    write_fn: Callable[[str], object],
    flush_fn: Callable[[], object],
    line_ending: str = "\n",
) -> str:
    """Read a secret one character at a time and echo a `*` mask."""
    buffer: list[str] = []
    write_fn(prompt)
    flush_fn()

    while True:
        char = read_char_fn()
        if char in ("\r", "\n"):
            write_fn(line_ending)
            flush_fn()
            return "".join(buffer)
        if char == "\x03":
            write_fn(line_ending)
            flush_fn()
            raise KeyboardInterrupt
        if char == "\x04":
            write_fn(line_ending)
            flush_fn()
            raise EOFError
        if char in ("\b", "\x08", "\x7f"):
            if buffer:
                buffer.pop()
                write_fn("\b \b")
                flush_fn()
            continue
        if not char or not char.isprintable():
            continue
        buffer.append(char)
        write_fn("*")
        flush_fn()


def _read_masked_secret_posix(prompt: str) -> str:
    import termios
    import tty

    stdin = sys.stdin
    stdout = sys.stdout
    if stdin is None or stdout is None:
        return getpass.getpass(prompt)

    fd = stdin.fileno()
    original_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return _read_masked_secret(
            prompt,
            read_char_fn=lambda: _read_posix_char(stdin),
            write_fn=stdout.write,
            flush_fn=stdout.flush,
            line_ending="",
        )
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)
        stdout.write("\r\n")
        stdout.flush()


def _read_posix_char(stdin) -> str:
    char = stdin.read(1)
    if char != "\x1b":
        return char

    _consume_posix_escape_sequence(stdin)
    return ""


def _consume_posix_escape_sequence(stdin) -> None:
    import select

    while True:
        ready, _, _ = select.select([stdin], [], [], 0.01)
        if not ready:
            return
        char = stdin.read(1)
        if not char:
            return
        if "\x40" <= char <= "\x7e":
            return


def _read_masked_secret_windows(prompt: str) -> str:
    import msvcrt

    stdout = sys.stdout
    if stdout is None:
        return getpass.getpass(prompt)

    def _read_char() -> str:
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()
            return ""
        return char

    return _read_masked_secret(
        prompt,
        read_char_fn=_read_char,
        write_fn=stdout.write,
        flush_fn=stdout.flush,
    )


def run_configuration_wizard(
    path: str,
    *,
    existing_config: AppConfig | None = None,
    input_fn: InputFn = input,
    secret_input_fn: SecretInputFn = _masked_secret_input,
    browser_opener: Callable[[str], bool] | None = None,
    print_fn: PrintFn = print,
) -> AppConfig:
    """Interactively collect config values and write the config file."""
    config = existing_config or build_default_config()

    print_fn("ChessShock setup")
    print_fn("This wizard will create or update {0}.".format(path))

    contact_email = _prompt_contact_email(
        "Contact email",
        default=_current_contact_email(config.user_agent),
        input_fn=input_fn,
        print_fn=print_fn,
    )
    config.user_agent = build_user_agent(contact_email)
    config.poll_interval_seconds = _prompt_int(
        "Poll interval in seconds",
        default=config.poll_interval_seconds,
        input_fn=input_fn,
        minimum=1,
    )

    auth_mode = _prompt_choice(
        "Lichess login method",
        default="oauth" if config.lichess.oauth.enabled else "token",
        choices=("oauth", "token"),
        input_fn=input_fn,
    )

    lichess_base_url = _prompt_string(
        "Lichess base URL",
        default=config.lichess.api_base_url,
        input_fn=input_fn,
    )

    if auth_mode == "oauth":
        oauth = _wizard_oauth_settings(config.lichess.oauth)
        print_fn(
            "Using the built-in OAuth callback at {0} with no extra OAuth scopes.".format(
                oauth.redirect_uri()
            )
        )
        print_fn("Opening Lichess OAuth in your browser.")
        temp_lichess = LichessConfig(
            api_token=None,
            api_base_url=lichess_base_url,
            oauth=oauth,
        )
        auth_preview = _format_auth_url_preview(temp_lichess)
        print_fn("If the browser does not open, visit:")
        print_fn(auth_preview)
        token = obtain_oauth_token(
            temp_lichess,
            user_agent=config.user_agent,
        ).access_token
        config.lichess = LichessConfig(
            api_token=token,
            api_base_url=lichess_base_url,
            oauth=oauth,
        )
    else:
        token = _prompt_secret(
            "Lichess personal access token",
            default=config.lichess.api_token or "",
            secret_input_fn=secret_input_fn,
        )
        config.lichess = LichessConfig(
            api_token=token,
            api_base_url=lichess_base_url,
            oauth=LichessOAuthConfig(enabled=False),
        )

    profile = _fetch_lichess_profile(config)
    username = _extract_profile_username(profile)
    if username is None:
        raise ConfigError("Could not determine the Lichess username for this token")
    config.username = username
    print_fn("Authenticated as {0}".format(username))

    config.openshock.enabled = _prompt_bool(
        "Enable OpenShock actions",
        default=config.openshock.enabled,
        input_fn=input_fn,
    )
    config.openshock.shocker_id = _prompt_string(
        "Default OpenShock shocker ID",
        default=config.openshock.shocker_id,
        input_fn=input_fn,
    )
    config.openshock.api_token = _prompt_openshock_api_token(
        default=config.openshock.api_token or "",
        secret_input_fn=secret_input_fn,
        browser_opener=browser_opener,
        print_fn=print_fn,
    )

    print_fn("Configure alerts")
    _prompt_turn_alert_settings(config, input_fn=input_fn)
    _prompt_loss_alert_settings(config, input_fn=input_fn)

    save_config(config, path)
    print_fn("Wrote {0}".format(Path(path)))
    return config


def refresh_lichess_oauth_token(
    config: AppConfig,
    path: str,
    *,
    print_fn: PrintFn = print,
) -> AppConfig:
    """Re-run the Lichess OAuth login and persist the updated token."""
    if not config.lichess.oauth.enabled:
        raise ConfigError("lichess.oauth.enabled must be true to use OAuth login")

    config.lichess.oauth = _wizard_oauth_settings(config.lichess.oauth)

    print_fn("Opening Lichess OAuth in your browser.")
    preview = _format_auth_url_preview(config.lichess)
    print_fn("If the browser does not open, visit:")
    print_fn(preview)

    token = obtain_oauth_token(
        config.lichess,
        user_agent=config.user_agent,
    ).access_token
    config.lichess.api_token = token

    profile = _fetch_lichess_profile(config)
    username = _extract_profile_username(profile)
    if username is not None:
        config.username = username

    save_config(config, path)
    print_fn("Updated {0}".format(Path(path)))
    return config


def _fetch_lichess_profile(config: AppConfig) -> dict:
    client = LichessClient(
        api_token=config.lichess.api_token or "",
        user_agent=config.user_agent,
        base_url=config.lichess.api_base_url,
    )
    try:
        return client.get_account_profile()
    except LichessError as exc:
        raise ConfigError("Could not verify the Lichess token: {0}".format(exc)) from exc


def _extract_profile_username(profile: dict) -> str | None:
    for key in ("username", "id"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _format_auth_url_preview(
    lichess: LichessConfig,
) -> str:
    from .oauth import create_authorization_request

    request = create_authorization_request(
        lichess,
        port=lichess.oauth.redirect_port or 51265,
    )
    return request.authorization_url


def _wizard_oauth_settings(defaults: LichessOAuthConfig) -> LichessOAuthConfig:
    """Return the built-in OAuth settings used by the setup wizard."""
    return LichessOAuthConfig(
        enabled=True,
        client_id=defaults.client_id,
        redirect_host=defaults.redirect_host,
        redirect_port=defaults.redirect_port,
        redirect_path=defaults.redirect_path,
        scopes=[],
    )


def _prompt_openshock_api_token(
    *,
    default: str,
    secret_input_fn: SecretInputFn,
    browser_opener: Callable[[str], bool] | None,
    print_fn: PrintFn,
) -> str:
    if not default:
        print_fn(
            "OpenShock applications currently use API tokens from the OpenShock dashboard."
        )
        print_fn("Opening the OpenShock API token page in your browser.")
        opener = browser_opener or webbrowser.open
        opener(DEFAULT_OPENSHOCK_TOKEN_PAGE_URL)
        print_fn("If the browser does not open, visit:")
        print_fn(DEFAULT_OPENSHOCK_TOKEN_PAGE_URL)

    return _prompt_secret(
        "OpenShock API token",
        default=default,
        secret_input_fn=secret_input_fn,
    )


def _prompt_turn_alert_settings(
    config: AppConfig,
    *,
    input_fn: InputFn,
) -> None:
    alert = config.alerts.turn
    alert.enabled = _prompt_bool(
        "Turn alert enabled",
        default=alert.enabled,
        input_fn=input_fn,
    )
    alert.action = _prompt_choice(
        "Turn alert action",
        default=alert.action,
        choices=("beep", "vibrate", "shock"),
        input_fn=input_fn,
    )
    alert.duration_ms = _prompt_int(
        "Turn alert duration (ms)",
        default=alert.duration_ms,
        input_fn=input_fn,
        minimum=300,
    )
    alert.intensity = _prompt_int_range(
        "Turn alert intensity",
        default=alert.intensity,
        input_fn=input_fn,
        minimum=0,
        maximum=100,
    )
    alert.cooldown_seconds = _prompt_int(
        "Turn alert cooldown (seconds)",
        default=alert.cooldown_seconds,
        input_fn=input_fn,
        minimum=0,
    )
    alert.only_on_new_turn = _prompt_bool(
        "Turn alert only on new turn",
        default=alert.only_on_new_turn,
        input_fn=input_fn,
    )
    alert.alert_on_startup = _prompt_bool(
        "Turn alert on startup",
        default=alert.alert_on_startup,
        input_fn=input_fn,
    )


def _prompt_loss_alert_settings(
    config: AppConfig,
    *,
    input_fn: InputFn,
) -> None:
    alert = config.alerts.loss
    alert.enabled = _prompt_bool(
        "Loss alert enabled",
        default=alert.enabled,
        input_fn=input_fn,
    )
    alert.action = _prompt_choice(
        "Loss alert action",
        default=alert.action,
        choices=("beep", "vibrate", "shock"),
        input_fn=input_fn,
    )
    alert.duration_ms = _prompt_int(
        "Loss alert duration (ms)",
        default=alert.duration_ms,
        input_fn=input_fn,
        minimum=300,
    )
    alert.intensity = _prompt_int_range(
        "Loss alert intensity",
        default=alert.intensity,
        input_fn=input_fn,
        minimum=0,
        maximum=100,
    )
    alert.cooldown_seconds = _prompt_int(
        "Loss alert cooldown (seconds)",
        default=alert.cooldown_seconds,
        input_fn=input_fn,
        minimum=0,
    )


def _prompt_choice(
    label: str,
    *,
    default: str,
    choices: tuple[str, ...],
    input_fn: InputFn,
) -> str:
    while True:
        response = input_fn(
            "{0} [{1}] ({2}): ".format(label, default, "/".join(choices))
        ).strip()
        if not response:
            return default
        if response in choices:
            return response


def _prompt_string(label: str, *, default: str, input_fn: InputFn) -> str:
    while True:
        response = input_fn("{0} [{1}]: ".format(label, default)).strip()
        if response:
            return response
        if default:
            return default


def _prompt_contact_email(
    label: str,
    *,
    default: str,
    input_fn: InputFn,
    print_fn: PrintFn,
) -> str:
    while True:
        value = _prompt_string(
            "{0}".format(label),
            default=default,
            input_fn=input_fn,
        )
        try:
            return validate_contact_email(value)
        except ConfigError as exc:
            print_fn(str(exc))


def _current_contact_email(user_agent: str) -> str:
    email = extract_contact_email(user_agent) or ""
    try:
        return validate_contact_email(email)
    except ConfigError:
        return "" if email == DEFAULT_CONTACT_EMAIL else email


def _prompt_optional_string(
    label: str,
    *,
    default: str,
    input_fn: InputFn,
) -> str:
    response = input_fn("{0} [{1}]: ".format(label, default)).strip()
    if response:
        return response
    return default


def _prompt_secret(
    label: str,
    *,
    default: str,
    secret_input_fn: SecretInputFn,
) -> str:
    while True:
        suffix = " [saved]" if default else ""
        response = secret_input_fn("{0}{1}: ".format(label, suffix)).strip()
        if response:
            return response
        if default:
            return default


def _prompt_int(
    label: str,
    *,
    default: int,
    input_fn: InputFn,
    minimum: int,
) -> int:
    while True:
        response = input_fn("{0} [{1}]: ".format(label, default)).strip()
        if not response:
            return default
        try:
            value = int(response)
        except ValueError:
            continue
        if value >= minimum:
            return value


def _prompt_int_range(
    label: str,
    *,
    default: int,
    input_fn: InputFn,
    minimum: int,
    maximum: int,
) -> int:
    while True:
        value = _prompt_int(
            label,
            default=default,
            input_fn=input_fn,
            minimum=minimum,
        )
        if value <= maximum:
            return value


def _prompt_bool(
    label: str,
    *,
    default: bool,
    input_fn: InputFn,
) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        response = input_fn("{0} [{1}]: ".format(label, default_text)).strip().lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
