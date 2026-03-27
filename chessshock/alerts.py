"""Shared alert dispatch logic for all event sources."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass


@dataclass
class AlertDispatch:
    """A single alert that was triggered."""

    event_type: str
    source: str
    reason: str
    action_sent: bool


class AlertManager:
    """Send OpenShock alerts and deduplicate them with cooldowns."""

    def __init__(
        self,
        config,
        openshock_client=None,
        time_fn=time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.openshock_client = openshock_client
        self.time_fn = time_fn
        self.logger = logger or logging.getLogger("chessshock")
        self._last_alert_at: dict[tuple[str, str], float] = {}

    def trigger(
        self,
        event_type: str,
        alert_config,
        source: str,
        reason: str,
    ) -> AlertDispatch | None:
        """Send an alert if the source is not currently on cooldown."""
        if not alert_config.enabled:
            return None

        cooldown_key = (event_type, source)
        previous_alert_time = self._last_alert_at.get(cooldown_key)
        now = self.time_fn()
        if (
            previous_alert_time is not None
            and alert_config.cooldown_seconds > 0
            and now - previous_alert_time < alert_config.cooldown_seconds
        ):
            remaining = alert_config.cooldown_seconds - (now - previous_alert_time)
            self.logger.info(
                "Skipping %s alert because cooldown is active for %.1fs (%s)",
                event_type,
                remaining,
                source,
            )
            return None

        shocker_id = alert_config.resolved_shocker_id(self.config.openshock.shocker_id)
        action_sent = False

        if not self.config.openshock.enabled or self.openshock_client is None:
            self.logger.warning(
                "Alert triggered with OpenShock disabled; would send event=%s action=%s shocker_id=%s reason=%s",
                event_type,
                alert_config.action,
                shocker_id,
                reason,
            )
        else:
            self.logger.warning(
                "Sending OpenShock alert event=%s action=%s shocker_id=%s reason=%s",
                event_type,
                alert_config.action,
                shocker_id,
                reason,
            )
            _send_openshock_action(
                client=self.openshock_client,
                action=alert_config.action,
                shocker_id=shocker_id,
                intensity=alert_config.intensity,
                duration_ms=alert_config.duration_ms,
            )
            action_sent = True

        self._last_alert_at[cooldown_key] = now
        return AlertDispatch(
            event_type=event_type,
            source=source,
            reason=reason,
            action_sent=action_sent,
        )


def _send_openshock_action(
    client,
    action: str,
    shocker_id: str,
    intensity: int,
    duration_ms: int,
) -> None:
    if action == "shock":
        client.shock(shocker_id, intensity=intensity, duration=duration_ms)
    elif action == "vibrate":
        client.vibrate(shocker_id, intensity=intensity, duration=duration_ms)
    else:
        client.beep(shocker_id, duration=duration_ms)
