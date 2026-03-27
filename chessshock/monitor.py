"""Polling and OpenShock trigger logic."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .alerts import AlertDispatch, AlertManager

RECENT_FINISHED_GAMES_LOOKBACK_MS = 60_000

NON_LOSS_STATUSES = {
    "aborted",
    "created",
    "draw",
    "insufficientmaterialclaim",
    "nostart",
    "stalemate",
    "started",
}


@dataclass
class CurrentGame:
    """A single active Lichess game."""

    game_id: str
    url: str
    is_user_turn: bool
    last_move: str | None
    speed: str | None
    variant: str | None
    seconds_left: int | None

    def summary(self) -> str:
        if self.is_user_turn:
            state = "your turn"
        else:
            state = "waiting"

        return (
            "game_id={0} state={1} speed={2} variant={3} seconds_left={4} last_move={5}".format(
                self.game_id,
                state,
                self.speed or "-",
                self.variant or "-",
                self.seconds_left if self.seconds_left is not None else "-",
                self.last_move or "-",
            )
        )


@dataclass
class FinishedGame:
    """A completed Lichess game."""

    game_id: str
    url: str
    user_color: str | None
    status: str | None
    winner: str | None
    last_move_at_ms: int | None
    speed: str | None
    variant: str | None

    def summary(self) -> str:
        return (
            "game_id={0} status={1} winner={2} speed={3} variant={4} last_move_at={5}".format(
                self.game_id,
                self.status or "-",
                self.winner or "-",
                self.speed or "-",
                self.variant or "-",
                _format_timestamp_ms(self.last_move_at_ms),
            )
        )


@dataclass
class PollResult:
    """Outcome of one polling cycle."""

    current_games: list[CurrentGame]
    user_turn_games: list[CurrentGame]
    loss_games: list[FinishedGame]
    alerts: list[AlertDispatch]


class ChessShockMonitor:
    """Poll Lichess and optionally send OpenShock actions."""

    def __init__(
        self,
        config,
        lichess_client,
        openshock_client=None,
        alert_manager: AlertManager | None = None,
        time_fn=time.monotonic,
        now_fn=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.lichess_client = lichess_client
        self.openshock_client = openshock_client
        self.time_fn = time_fn
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.logger = logger or logging.getLogger("chessshock")
        self.alert_manager = alert_manager or AlertManager(
            config=config,
            openshock_client=openshock_client,
            time_fn=time_fn,
            logger=self.logger,
        )
        self.username_lower = config.username.lower()
        self._startup_time_ms = _datetime_to_ms(self.now_fn())
        self._first_poll = True
        self._user_turn_ids: set[str] = set()
        self._finished_game_ids: set[str] = set()
        self._last_finished_check_ms = self._startup_time_ms

    def poll_once(self) -> PollResult:
        """Run one Lichess poll and send any matching alerts."""
        current_games = self.fetch_current_games()
        user_turn_games = [game for game in current_games if game.is_user_turn]
        user_turn_ids = {game.game_id for game in user_turn_games}

        self._log_current_games(current_games)

        alerts: list[AlertDispatch] = []

        turn_alert = self._maybe_trigger_turn_alert(user_turn_games, user_turn_ids)
        if turn_alert is not None:
            alerts.append(turn_alert)

        finished_games_by_id = self._fetch_recent_finished_games()
        loss_games = self._detect_loss_games(finished_games_by_id)
        for finished_game in loss_games:
            alert = self.alert_manager.trigger(
                event_type="loss",
                alert_config=self.config.alerts.loss,
                source=finished_game.game_id,
                reason="game ended in a loss (status={0}, winner={1})".format(
                    finished_game.status or "unknown",
                    finished_game.winner or "unknown",
                ),
            )
            if alert is not None:
                alerts.append(alert)

        self._user_turn_ids = user_turn_ids
        self._finished_game_ids.update(finished_games_by_id)
        self._first_poll = False

        return PollResult(
            current_games=current_games,
            user_turn_games=user_turn_games,
            loss_games=loss_games,
            alerts=alerts,
        )

    def fetch_current_games(self) -> list[CurrentGame]:
        """Fetch all active Lichess games for the configured player."""
        games = []

        for game in self.lichess_client.get_ongoing_games(max_games=50):
            game_id = _clean_string(game.get("gameId")) or _clean_string(game.get("id"))
            if not game_id:
                continue

            current_game = CurrentGame(
                game_id=game_id,
                url="{0}/{1}".format(
                    self.config.lichess.api_base_url.rstrip("/"),
                    game_id,
                ),
                is_user_turn=bool(game.get("isMyTurn")),
                last_move=_clean_string(game.get("lastMove")),
                speed=_clean_string(game.get("speed")),
                variant=_extract_variant_key(game.get("variant")),
                seconds_left=_coerce_int(game.get("secondsLeft")),
            )
            games.append(current_game)

        games.sort(
            key=lambda game: (
                not game.is_user_turn,
                game.seconds_left if game.seconds_left is not None else 10**12,
                game.game_id,
            )
        )
        return games

    def _maybe_trigger_turn_alert(
        self,
        user_turn_games: list[CurrentGame],
        user_turn_ids: set[str],
    ) -> AlertDispatch | None:
        alert_config = self.config.alerts.turn
        if not alert_config.enabled:
            return None
        if not user_turn_games:
            self.logger.info("No games are currently waiting for your move")
            return None

        if self._first_poll and not alert_config.alert_on_startup:
            self.logger.info("Startup check skipped existing turn alerts")
            return None

        if alert_config.only_on_new_turn:
            if self._first_poll:
                source_ids = user_turn_ids
                reason = "{0} game(s) are already waiting for your move at startup".format(
                    len(user_turn_games)
                )
            else:
                source_ids = user_turn_ids - self._user_turn_ids
                if not source_ids:
                    self.logger.info("No new games became your turn")
                    return None
                reason = "{0} game(s) just became your turn".format(len(source_ids))
            source = ",".join(sorted(source_ids))
        else:
            source = "turn-reminder"
            reason = "{0} game(s) are waiting for your move".format(len(user_turn_games))

        return self.alert_manager.trigger(
            event_type="turn",
            alert_config=alert_config,
            source=source,
            reason=reason,
        )

    def _detect_loss_games(
        self,
        finished_games_by_id: dict[str, FinishedGame],
    ) -> list[FinishedGame]:
        if not self.config.alerts.loss.enabled:
            return []
        if self._first_poll:
            new_finished_ids = {
                game_id
                for game_id, finished_game in finished_games_by_id.items()
                if _finished_within_window(
                    finished_game,
                    reference_ms=self._startup_time_ms,
                    window_ms=self._finished_games_lookback_ms(),
                )
            }
        else:
            new_finished_ids = set(finished_games_by_id) - self._finished_game_ids
        if not new_finished_ids:
            return []

        losses = []
        for game_id in sorted(new_finished_ids):
            finished_game = finished_games_by_id[game_id]
            if _did_user_lose(finished_game):
                self.logger.warning("Detected lost game: %s", finished_game.summary())
                losses.append(finished_game)
            else:
                self.logger.info(
                    "New finished game did not end in a loss: %s",
                    finished_game.summary(),
                )

        return losses

    def _fetch_recent_finished_games(self) -> dict[str, FinishedGame]:
        if not self.config.alerts.loss.enabled:
            return {}

        overlap_ms = self._finished_games_lookback_ms()
        since_ms = max(0, self._last_finished_check_ms - overlap_ms)
        now_ms = _datetime_to_ms(self.now_fn())
        games = self.lichess_client.get_recent_games(
            self.config.username,
            since_ms=since_ms,
            max_games=100,
        )
        self._last_finished_check_ms = now_ms

        finished_games: dict[str, FinishedGame] = {}
        for game in games:
            finished_game = self._build_finished_game(game)
            if finished_game is None:
                continue
            if finished_game.status in {"created", "started"}:
                continue
            finished_games[finished_game.game_id] = finished_game

        return finished_games

    def _build_finished_game(self, game: dict) -> FinishedGame | None:
        game_id = _clean_string(game.get("id"))
        if not game_id:
            return None

        players = game.get("players")
        user_color = _resolve_user_color(players, self.username_lower)

        return FinishedGame(
            game_id=game_id,
            url="{0}/{1}".format(
                self.config.lichess.api_base_url.rstrip("/"),
                game_id,
            ),
            user_color=user_color,
            status=_extract_status_name(game.get("status")),
            winner=_normalize_color(game.get("winner")),
            last_move_at_ms=_coerce_int(game.get("lastMoveAt")),
            speed=_clean_string(game.get("speed")),
            variant=_extract_variant_key(game.get("variant")),
        )

    def _log_current_games(self, current_games: list[CurrentGame]) -> None:
        self.logger.info("Active Lichess games: %s", len(current_games))
        for game in current_games:
            self.logger.info("  %s", game.summary())

    def _finished_games_lookback_ms(self) -> int:
        return max(
            RECENT_FINISHED_GAMES_LOOKBACK_MS,
            self.config.poll_interval_seconds * 1000 * 3,
        )


def _resolve_user_color(players, username_lower: str) -> str | None:
    if not isinstance(players, dict):
        return None

    for color in ("white", "black"):
        entry = players.get(color)
        if not isinstance(entry, dict):
            continue

        user = entry.get("user")
        if isinstance(user, dict):
            user_id = _clean_string(user.get("id"))
            user_name = _clean_string(user.get("name"))
            if (
                user_id is not None and user_id.lower() == username_lower
            ) or (
                user_name is not None and user_name.lower() == username_lower
            ):
                return color

        entry_name = _clean_string(entry.get("name"))
        if entry_name is not None and entry_name.lower() == username_lower:
            return color

    return None


def _extract_variant_key(value) -> str | None:
    if isinstance(value, dict):
        return _clean_string(value.get("key")) or _clean_string(value.get("name"))
    return _clean_string(value)


def _extract_status_name(value) -> str | None:
    if isinstance(value, dict):
        return _clean_string(value.get("name"))
    return _clean_string(value)


def _normalize_color(value) -> str | None:
    text = _clean_string(value)
    if text in {"white", "black"}:
        return text
    return None


def _did_user_lose(game: FinishedGame) -> bool:
    if game.user_color not in {"white", "black"}:
        return False
    status = (game.status or "").strip().lower()
    if status in NON_LOSS_STATUSES:
        return False
    if game.winner in {"white", "black"}:
        return game.winner != game.user_color
    return False


def _finished_within_window(
    game: FinishedGame,
    *,
    reference_ms: int,
    window_ms: int,
) -> bool:
    if game.last_move_at_ms is None:
        return False
    return game.last_move_at_ms >= max(0, reference_ms - window_ms)


def _clean_string(value) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _coerce_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _format_timestamp_ms(value: int | None) -> str:
    if value is None:
        return "-"
    return (
        datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        .astimezone()
        .replace(microsecond=0)
        .isoformat()
    )
