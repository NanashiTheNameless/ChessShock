"""Config loading, validation, and serialization for ChessShock."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "ChessShock"
DEFAULT_CONFIG_FILENAME = "config.json"
USER_AGENT_TEMPLATE = "ChessShock/0.0.2 (contact: {email})"
DEFAULT_CONTACT_EMAIL = "replace-this-with-your-real-email@example.com"
DEFAULT_USER_AGENT = USER_AGENT_TEMPLATE.format(email=DEFAULT_CONTACT_EMAIL)
DEFAULT_LICHESS_BASE_URL = "https://lichess.org"
DEFAULT_OPENSHOCK_TOKEN_PAGE_URL = "https://next.openshock.app/settings/api-tokens"
DEFAULT_OAUTH_CLIENT_ID = "chessshock-cli"
DEFAULT_OAUTH_REDIRECT_HOST = "127.0.0.1"
DEFAULT_OAUTH_REDIRECT_PORT = 51265
DEFAULT_OAUTH_REDIRECT_PATH = "/oauth/callback"
VALID_ACTIONS = {"shock", "vibrate", "beep"}
CONTACT_EMAIL_PATTERN = re.compile(
    r"(?P<email>[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63})",
    re.IGNORECASE,
)
PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "example.invalid",
    "localhost",
    "local",
}
PLACEHOLDER_EMAIL_SUFFIXES = (
    ".example",
    ".invalid",
    ".localhost",
    ".test",
)


class ConfigError(ValueError):
    """Raised when the application config is invalid."""


@dataclass
class LichessOAuthConfig:
    """OAuth settings for authenticating with Lichess."""

    enabled: bool = False
    client_id: str = DEFAULT_OAUTH_CLIENT_ID
    redirect_host: str = DEFAULT_OAUTH_REDIRECT_HOST
    redirect_port: int = DEFAULT_OAUTH_REDIRECT_PORT
    redirect_path: str = DEFAULT_OAUTH_REDIRECT_PATH
    scopes: list[str] = field(default_factory=list)

    def redirect_uri(self, port: int | None = None) -> str:
        """Build the redirect URI for the configured callback server."""
        actual_port = self.redirect_port if port is None else port
        return "http://{0}:{1}{2}".format(
            self.redirect_host,
            actual_port,
            self.redirect_path,
        )


@dataclass
class LichessConfig:
    """Settings for authenticating to the Lichess API."""

    api_token: str | None = None
    api_base_url: str = DEFAULT_LICHESS_BASE_URL
    oauth: LichessOAuthConfig = field(default_factory=LichessOAuthConfig)


@dataclass
class OpenShockConfig:
    """Base settings for OpenShock actions."""

    enabled: bool = False
    api_token: str | None = None
    shocker_id: str = "all"

    def resolved_api_token(self) -> str | None:
        """Return the configured OpenShock API token."""
        return self.api_token

    def resolved_api_key(self) -> str | None:
        """Backward-compatible alias for older internal callers."""
        return self.resolved_api_token()


@dataclass
class EventAlertConfig:
    """Base action settings for an alert."""

    enabled: bool = False
    action: str = "beep"
    shocker_id: str | None = None
    intensity: int = 0
    duration_ms: int = 500
    cooldown_seconds: int = 5

    def resolved_shocker_id(self, app_shocker_id: str) -> str:
        """Return the configured shocker id or the app default."""
        return self.shocker_id or app_shocker_id


@dataclass
class TurnAlertConfig(EventAlertConfig):
    """Settings for alerts when it becomes the player's turn."""

    action: str = "beep"
    only_on_new_turn: bool = True
    alert_on_startup: bool = True
    cooldown_seconds: int = 5


@dataclass
class LossAlertConfig(EventAlertConfig):
    """Settings for alerts when a tracked game ends in a loss."""

    action: str = "shock"
    intensity: int = 45
    duration_ms: int = 800


@dataclass
class AlertsConfig:
    """All supported alert types."""

    turn: TurnAlertConfig = field(default_factory=TurnAlertConfig)
    loss: LossAlertConfig = field(default_factory=LossAlertConfig)


@dataclass
class AppConfig:
    """Top-level application config."""

    username: str
    user_agent: str
    lichess: LichessConfig
    poll_interval_seconds: int = 5
    openshock: OpenShockConfig = field(default_factory=OpenShockConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)


def default_config_dir() -> Path:
    """Return the platform-appropriate config directory for ChessShock."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / APP_NAME

    return Path.home() / ".config" / APP_NAME


def default_config_path() -> Path:
    """Return the default path to the user's ChessShock config file."""
    return default_config_dir() / DEFAULT_CONFIG_FILENAME


def build_default_config() -> AppConfig:
    """Return a writable default config for interactive setup."""
    return AppConfig(
        username="your_lichess_username",
        user_agent=DEFAULT_USER_AGENT,
        lichess=LichessConfig(
            api_token=None,
            api_base_url=DEFAULT_LICHESS_BASE_URL,
            oauth=LichessOAuthConfig(enabled=True),
        ),
        poll_interval_seconds=5,
        openshock=OpenShockConfig(
            enabled=False,
            api_token=None,
            shocker_id="all",
        ),
        alerts=AlertsConfig(
            turn=TurnAlertConfig(
                enabled=True,
                action="vibrate",
                duration_ms=600,
                intensity=100,
                only_on_new_turn=True,
                alert_on_startup=True,
                cooldown_seconds=5,
            ),
            loss=LossAlertConfig(
                enabled=True,
                action="shock",
                intensity=45,
                duration_ms=800,
                cooldown_seconds=5,
            ),
        ),
    )


def load_config(path: str) -> AppConfig:
    """Read a config file from disk."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError("Config file was not found: {0}".format(config_path))

    try:
        raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "Config file is not valid JSON: {0}".format(config_path)
        ) from exc

    if not isinstance(raw_data, dict):
        raise ConfigError("Config root must be a JSON object")

    lichess_data = _as_dict(raw_data.get("lichess", {}), "lichess")
    lichess_oauth_data = _as_dict(lichess_data.get("oauth", {}), "lichess.oauth")
    openshock_data = _as_dict(raw_data.get("openshock", {}), "openshock")
    alerts_data = _as_dict(raw_data.get("alerts", {}), "alerts")
    legacy_trigger = _as_dict(raw_data.get("trigger", {}), "trigger")

    openshock = OpenShockConfig(
        enabled=_as_bool(openshock_data.get("enabled", False), "openshock.enabled"),
        api_token=_optional_string(
            openshock_data.get("api_token", openshock_data.get("api_key")),
            "openshock.api_token",
        ),
        shocker_id=_required_string(
            openshock_data.get(
                "shocker_id",
                openshock_data.get("default_shocker_id", "all"),
            ),
            "openshock.shocker_id",
        ),
    )

    config = AppConfig(
        username=_required_string(raw_data.get("username"), "username"),
        user_agent=_required_string(raw_data.get("user_agent"), "user_agent"),
        lichess=LichessConfig(
            api_token=_optional_string(
                lichess_data.get("api_token"),
                "lichess.api_token",
            ),
            api_base_url=_required_string(
                lichess_data.get("api_base_url", DEFAULT_LICHESS_BASE_URL),
                "lichess.api_base_url",
            ),
            oauth=LichessOAuthConfig(
                enabled=_as_bool(
                    lichess_oauth_data.get("enabled", False),
                    "lichess.oauth.enabled",
                ),
                client_id=_required_string(
                    lichess_oauth_data.get("client_id", DEFAULT_OAUTH_CLIENT_ID),
                    "lichess.oauth.client_id",
                ),
                redirect_host=_required_string(
                    lichess_oauth_data.get(
                        "redirect_host",
                        DEFAULT_OAUTH_REDIRECT_HOST,
                    ),
                    "lichess.oauth.redirect_host",
                ),
                redirect_port=_as_int(
                    lichess_oauth_data.get(
                        "redirect_port",
                        DEFAULT_OAUTH_REDIRECT_PORT,
                    ),
                    "lichess.oauth.redirect_port",
                ),
                redirect_path=_required_string(
                    lichess_oauth_data.get(
                        "redirect_path",
                        DEFAULT_OAUTH_REDIRECT_PATH,
                    ),
                    "lichess.oauth.redirect_path",
                ),
                scopes=_as_string_list(
                    lichess_oauth_data.get("scopes", []),
                    "lichess.oauth.scopes",
                ),
            ),
        ),
        poll_interval_seconds=_as_int(
            raw_data.get("poll_interval_seconds", 5),
            "poll_interval_seconds",
        ),
        openshock=openshock,
        alerts=AlertsConfig(
            turn=_load_turn_alert(
                _as_dict(alerts_data.get("turn", {}), "alerts.turn"),
                legacy_trigger=legacy_trigger,
                legacy_openshock=openshock_data,
            ),
            loss=_load_loss_alert(
                _as_dict(alerts_data.get("loss", {}), "alerts.loss")
            ),
        ),
    )

    _validate_config(config)
    return config


def save_config(config: AppConfig, path: str) -> None:
    """Write a config file to disk."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config_to_dict(config), indent=2) + "\n",
        encoding="utf-8",
    )


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Serialize a config object to a JSON-friendly dict."""
    return {
        "username": config.username,
        "user_agent": config.user_agent,
        "poll_interval_seconds": config.poll_interval_seconds,
        "lichess": {
            "api_token": config.lichess.api_token or "",
            "api_base_url": config.lichess.api_base_url,
            "oauth": {
                "enabled": config.lichess.oauth.enabled,
                "client_id": config.lichess.oauth.client_id,
                "redirect_host": config.lichess.oauth.redirect_host,
                "redirect_port": config.lichess.oauth.redirect_port,
                "redirect_path": config.lichess.oauth.redirect_path,
                "scopes": list(config.lichess.oauth.scopes),
            },
        },
        "openshock": {
            "enabled": config.openshock.enabled,
            "api_token": config.openshock.api_token or "",
            "shocker_id": config.openshock.shocker_id,
        },
        "alerts": {
            "turn": _event_alert_to_dict(
                config.alerts.turn,
                extra={
                    "only_on_new_turn": config.alerts.turn.only_on_new_turn,
                    "alert_on_startup": config.alerts.turn.alert_on_startup,
                },
            ),
            "loss": _event_alert_to_dict(config.alerts.loss),
        },
    }


def _event_alert_to_dict(
    alert: EventAlertConfig,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {
        "enabled": alert.enabled,
        "action": alert.action,
        "duration_ms": alert.duration_ms,
        "intensity": alert.intensity,
        "cooldown_seconds": alert.cooldown_seconds,
    }
    if alert.shocker_id is not None:
        data["shocker_id"] = alert.shocker_id
    if extra:
        data.update(extra)
    return data


def _load_turn_alert(
    data: dict[str, Any],
    legacy_trigger: dict[str, Any],
    legacy_openshock: dict[str, Any],
) -> TurnAlertConfig:
    default_action = str(
        data.get("action", legacy_openshock.get("action", "beep"))
    ).strip().lower() or "beep"
    default_intensity = legacy_openshock.get(
        "intensity",
        0 if default_action == "beep" else 35,
    )
    default_duration = legacy_openshock.get("duration_ms", 500)

    return TurnAlertConfig(
        enabled=_as_bool(
            data.get("enabled", legacy_openshock.get("enabled", False)),
            "alerts.turn.enabled",
        ),
        action=_required_string(default_action, "alerts.turn.action").lower(),
        shocker_id=_optional_string(
            data.get("shocker_id", legacy_openshock.get("shocker_id")),
            "alerts.turn.shocker_id",
        ),
        intensity=_as_int(
            data.get("intensity", default_intensity),
            "alerts.turn.intensity",
        ),
        duration_ms=_as_int(
            data.get("duration_ms", default_duration),
            "alerts.turn.duration_ms",
        ),
        cooldown_seconds=_as_int(
            data.get("cooldown_seconds", legacy_trigger.get("cooldown_seconds", 5)),
            "alerts.turn.cooldown_seconds",
        ),
        only_on_new_turn=_as_bool(
            data.get(
                "only_on_new_turn",
                legacy_trigger.get("only_on_new_games", True),
            ),
            "alerts.turn.only_on_new_turn",
        ),
        alert_on_startup=_as_bool(
            data.get(
                "alert_on_startup",
                legacy_trigger.get("shock_on_startup_if_games_waiting", False),
            ),
            "alerts.turn.alert_on_startup",
        ),
    )


def _load_loss_alert(data: dict[str, Any]) -> LossAlertConfig:
    return LossAlertConfig(
        enabled=_as_bool(data.get("enabled", False), "alerts.loss.enabled"),
        action=_required_string(data.get("action", "shock"), "alerts.loss.action").lower(),
        shocker_id=_optional_string(
            data.get("shocker_id"),
            "alerts.loss.shocker_id",
        ),
        intensity=_as_int(data.get("intensity", 45), "alerts.loss.intensity"),
        duration_ms=_as_int(
            data.get("duration_ms", 800),
            "alerts.loss.duration_ms",
        ),
        cooldown_seconds=_as_int(
            data.get("cooldown_seconds", 5),
            "alerts.loss.cooldown_seconds",
        ),
    )


def _validate_config(config: AppConfig) -> None:
    if config.poll_interval_seconds <= 0:
        raise ConfigError("poll_interval_seconds must be greater than 0")
    if not config.openshock.shocker_id:
        raise ConfigError("openshock.shocker_id must be a non-empty string")
    if not config.lichess.api_base_url.startswith(("http://", "https://")):
        raise ConfigError("lichess.api_base_url must start with http:// or https://")

    validate_user_agent(config.user_agent)
    _validate_lichess_auth(config.lichess)
    _validate_alert(config.alerts.turn, "alerts.turn")
    _validate_alert(config.alerts.loss, "alerts.loss")

    if config.openshock.enabled and _any_alert_enabled(config.alerts):
        if not config.openshock.resolved_api_token():
            raise ConfigError(
                "OpenShock is enabled but no API token was provided in config"
            )


def _validate_lichess_auth(lichess: LichessConfig) -> None:
    if lichess.api_token:
        if len(lichess.api_token) < 5:
            raise ConfigError("lichess.api_token looks too short")
        return

    if not lichess.oauth.enabled:
        raise ConfigError(
            "Provide lichess.api_token or enable lichess.oauth.enabled for interactive login"
        )

    oauth = lichess.oauth
    if oauth.redirect_port < 0 or oauth.redirect_port > 65535:
        raise ConfigError("lichess.oauth.redirect_port must be between 0 and 65535")
    if not oauth.redirect_path.startswith("/"):
        raise ConfigError("lichess.oauth.redirect_path must start with /")
    for scope in oauth.scopes:
        if not scope or any(char.isspace() for char in scope):
            raise ConfigError("lichess.oauth.scopes must contain non-empty scope names")


def validate_user_agent(user_agent: str) -> str:
    """Ensure the User-Agent contains a non-placeholder contact email."""
    email = extract_contact_email(user_agent)
    if email is None:
        raise ConfigError(
            "user_agent must include a contact email address, for example `ChessShock/0.0.2 (contact: you@yourdomain.com)`"
        )

    validate_contact_email(email)
    return user_agent


def build_user_agent(contact_email: str) -> str:
    """Build the standard ChessShock User-Agent from a contact email."""
    email = validate_contact_email(contact_email)
    return USER_AGENT_TEMPLATE.format(email=email)


def extract_contact_email(user_agent: str) -> str | None:
    """Extract the first contact email from a User-Agent string."""
    match = CONTACT_EMAIL_PATTERN.search(user_agent)
    if match is None:
        return None
    return match.group("email")


def validate_contact_email(contact_email: str) -> str:
    """Ensure the contact email is syntactically valid and not a placeholder."""
    stripped = contact_email.strip()
    if not CONTACT_EMAIL_PATTERN.fullmatch(stripped):
        raise ConfigError(
            "A real contact email address is required, for example `you@yourdomain.com`"
        )

    domain = stripped.lower().rsplit("@", 1)[-1]
    if domain in PLACEHOLDER_EMAIL_DOMAINS or domain.endswith(PLACEHOLDER_EMAIL_SUFFIXES):
        raise ConfigError(
            "A real contact email address is required; placeholder addresses like `you@example.com` are not allowed"
        )

    return stripped


def _validate_alert(alert: EventAlertConfig, field_name: str) -> None:
    if alert.action not in VALID_ACTIONS:
        raise ConfigError(
            "{0}.action must be one of: {1}".format(
                field_name,
                ", ".join(sorted(VALID_ACTIONS)),
            )
        )
    if alert.cooldown_seconds < 0:
        raise ConfigError(
            "{0}.cooldown_seconds must be 0 or greater".format(field_name)
        )
    if not 0 <= alert.intensity <= 100:
        raise ConfigError("{0}.intensity must be between 0 and 100".format(field_name))
    if not 300 <= alert.duration_ms <= 65535:
        raise ConfigError(
            "{0}.duration_ms must be between 300 and 65535".format(field_name)
        )


def _any_alert_enabled(alerts: AlertsConfig) -> bool:
    return alerts.turn.enabled or alerts.loss.enabled


def _as_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("{0} must be a JSON object".format(field_name))
    return value


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("{0} must be a non-empty string".format(field_name))
    return value.strip()


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("{0} must be a string".format(field_name))
    stripped = value.strip()
    return stripped or None


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("{0} must be true or false".format(field_name))
    return value


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("{0} must be an integer".format(field_name))
    return value


def _as_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError("{0} must be a JSON array".format(field_name))
    items = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                "{0}[{1}] must be a non-empty string".format(field_name, index)
            )
        items.append(entry.strip())
    return items
