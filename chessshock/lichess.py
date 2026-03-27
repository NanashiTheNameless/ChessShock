"""Small Lichess API client used by ChessShock."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class LichessError(RuntimeError):
    """Raised when a Lichess API request fails."""


class LichessRateLimitError(LichessError):
    """Raised when the Lichess API returns HTTP 429."""

    def __init__(self, retry_after_seconds: int = 60) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            "Lichess API rate limit reached; wait {0}s before retrying".format(
                retry_after_seconds
            )
        )


class LichessClient:
    """Minimal authenticated client for the endpoints ChessShock needs."""

    def __init__(
        self,
        api_token: str,
        user_agent: str,
        base_url: str = "https://lichess.org",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_token = api_token
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_ongoing_games(self, max_games: int = 50) -> list[dict[str, Any]]:
        """Fetch the current user's ongoing Lichess games."""
        payload = self._request_json(
            "/api/account/playing",
            params={"nb": max_games},
            accept="application/json",
        )
        games = payload.get("nowPlaying", [])
        if not isinstance(games, list):
            raise LichessError("Lichess response was missing nowPlaying")
        return [game for game in games if isinstance(game, dict)]

    def get_account_profile(self) -> dict[str, Any]:
        """Fetch the logged-in user's profile."""
        return self._request_json(
            "/api/account",
            params=None,
            accept="application/json",
        )

    def get_recent_games(
        self,
        username: str,
        since_ms: int,
        max_games: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch recently finished games for the configured user."""
        return self._request_ndjson(
            "/api/games/user/{0}".format(username),
            params={
                "since": max(0, since_ms),
                "max": max_games,
                "moves": "false",
                "tags": "false",
                "pgnInJson": "false",
                "clocks": "false",
                "evals": "false",
                "opening": "false",
            },
        )

    def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None,
        accept: str,
    ) -> dict[str, Any]:
        request = self._build_request(path, params=params, accept=accept)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code == 429:
                raise LichessRateLimitError(_retry_after_seconds(exc)) from exc
            raise LichessError(
                "Lichess API request failed with HTTP {0}: {1}".format(
                    exc.code,
                    exc.reason,
                )
            ) from exc
        except URLError as exc:
            raise LichessError("Could not reach Lichess API: {0}".format(exc.reason)) from exc
        except json.JSONDecodeError as exc:
            raise LichessError("Lichess API returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise LichessError("Lichess API returned an unexpected JSON payload")
        return payload

    def _request_ndjson(
        self,
        path: str,
        *,
        params: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        request = self._build_request(
            path,
            params=params,
            accept="application/x-ndjson",
        )
        rows: list[dict[str, Any]] = []
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    if isinstance(item, dict):
                        rows.append(item)
        except HTTPError as exc:
            if exc.code == 429:
                raise LichessRateLimitError(_retry_after_seconds(exc)) from exc
            raise LichessError(
                "Lichess API request failed with HTTP {0}: {1}".format(
                    exc.code,
                    exc.reason,
                )
            ) from exc
        except URLError as exc:
            raise LichessError("Could not reach Lichess API: {0}".format(exc.reason)) from exc
        except json.JSONDecodeError as exc:
            raise LichessError("Lichess API returned invalid NDJSON") from exc

        return rows

    def _build_request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None,
        accept: str,
    ) -> Request:
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)

        return Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": "Bearer {0}".format(self.api_token),
                "User-Agent": self.user_agent,
            },
        )


def _retry_after_seconds(exc: HTTPError) -> int:
    value = exc.headers.get("Retry-After") if exc.headers is not None else None
    if value is not None:
        try:
            parsed = int(value.strip())
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return 60
