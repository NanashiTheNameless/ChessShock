"""Lichess OAuth helpers for terminal-based login."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import LichessConfig


class LichessOAuthError(RuntimeError):
    """Raised when a Lichess OAuth flow fails."""


@dataclass
class OAuthTokenResult:
    """Result of a successful OAuth token exchange."""

    access_token: str
    expires_in: int
    token_type: str


@dataclass
class OAuthAuthorizationRequest:
    """PKCE authorization request values."""

    authorization_url: str
    code_verifier: str
    state: str
    redirect_uri: str


def create_authorization_request(
    lichess: LichessConfig,
    *,
    username_hint: str | None = None,
    port: int | None = None,
) -> OAuthAuthorizationRequest:
    """Build a PKCE authorization request for the configured Lichess app."""
    code_verifier = _generate_code_verifier()
    state = secrets.token_urlsafe(24)
    redirect_uri = lichess.oauth.redirect_uri(port=port)
    code_challenge = _build_code_challenge(code_verifier)
    authorization_url = build_authorization_url(
        base_url=lichess.api_base_url,
        client_id=lichess.oauth.client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        scopes=lichess.oauth.scopes,
        username_hint=username_hint,
    )
    return OAuthAuthorizationRequest(
        authorization_url=authorization_url,
        code_verifier=code_verifier,
        state=state,
        redirect_uri=redirect_uri,
    )


def build_authorization_url(
    *,
    base_url: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: list[str],
    username_hint: str | None = None,
) -> str:
    """Build the Lichess OAuth authorization URL."""
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "state": state,
    }
    if scopes:
        query["scope"] = " ".join(scopes)
    if username_hint:
        query["username"] = username_hint
    return "{0}/oauth?{1}".format(base_url.rstrip("/"), urlencode(query))


def obtain_oauth_token(
    lichess: LichessConfig,
    *,
    user_agent: str,
    username_hint: str | None = None,
    timeout_seconds: float = 300.0,
    open_browser: bool = True,
    browser_opener: Callable[[str], bool] | None = None,
) -> OAuthTokenResult:
    """Run an interactive Lichess OAuth login and exchange the code for a token."""
    callback = _CallbackState()
    server = _start_callback_server(
        host=lichess.oauth.redirect_host,
        port=lichess.oauth.redirect_port,
        redirect_path=lichess.oauth.redirect_path,
        callback=callback,
    )
    actual_port = server.server_port
    auth_request = create_authorization_request(
        lichess,
        username_hint=username_hint,
        port=actual_port,
    )

    try:
        if open_browser:
            opener = browser_opener or webbrowser.open
            opener(auth_request.authorization_url)

        if not callback.event.wait(timeout_seconds):
            raise LichessOAuthError(
                "Timed out waiting for the Lichess OAuth callback"
            )

        params = callback.params or {}
        if params.get("state") != auth_request.state:
            raise LichessOAuthError("Returned OAuth state did not match the request")
        if "error" in params:
            raise LichessOAuthError(
                "Lichess OAuth failed: {0}".format(
                    params.get("error_description") or params["error"]
                )
            )

        code = params.get("code")
        if not code:
            raise LichessOAuthError("Lichess OAuth callback did not include a code")

        return exchange_authorization_code(
            base_url=lichess.api_base_url,
            client_id=lichess.oauth.client_id,
            redirect_uri=auth_request.redirect_uri,
            code=code,
            code_verifier=auth_request.code_verifier,
            user_agent=user_agent,
        )
    finally:
        server.shutdown()
        server.server_close()


def exchange_authorization_code(
    *,
    base_url: str,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    user_agent: str,
) -> OAuthTokenResult:
    """Exchange a Lichess OAuth authorization code for an access token."""
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        }
    ).encode("utf-8")
    request = Request(
        "{0}/api/token".format(base_url.rstrip("/")),
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": user_agent,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30.0) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = exc.reason
        try:
            body_text = exc.read().decode("utf-8")
            payload = json.loads(body_text)
            detail = payload.get("error_description") or payload.get("error") or detail
        except Exception:
            pass
        raise LichessOAuthError(
            "Failed to obtain Lichess access token: {0}".format(detail)
        ) from exc
    except URLError as exc:
        raise LichessOAuthError(
            "Could not reach the Lichess token endpoint: {0}".format(exc.reason)
        ) from exc
    except json.JSONDecodeError as exc:
        raise LichessOAuthError("Lichess token endpoint returned invalid JSON") from exc

    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise LichessOAuthError("Lichess token response did not include access_token")
    if not isinstance(token_type, str) or not token_type:
        raise LichessOAuthError("Lichess token response did not include token_type")
    if not isinstance(expires_in, int):
        raise LichessOAuthError("Lichess token response did not include expires_in")

    return OAuthTokenResult(
        access_token=access_token,
        expires_in=expires_in,
        token_type=token_type,
    )


@dataclass
class _CallbackState:
    event: threading.Event = field(default_factory=threading.Event)
    params: dict[str, str] | None = None


def _start_callback_server(
    *,
    host: str,
    port: int,
    redirect_path: str,
    callback: _CallbackState,
) -> ThreadingHTTPServer:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != redirect_path:
                self.send_response(404)
                self.end_headers()
                return

            query = parse_qs(parsed.query, keep_blank_values=True)
            callback.params = {key: values[-1] for key, values in query.items()}
            callback.event.set()

            body = _build_callback_success_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:  # noqa: A003
            del format, args

    server = ThreadingHTTPServer((host, port), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _build_callback_success_page() -> bytes:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>ChessShock setup complete</title>
  <style>
    :root {
      color-scheme: dark;
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      min-height: 100%;
      background-color: #060606;
      background-image: conic-gradient(#181818 25%, #060606 0 50%, #181818 0 75%, #060606 0);
      background-size: 96px 96px;
      color: #fff;
      font-family: "Courier New", Courier, monospace;
    }

    body {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
    }

    main {
      width: min(100%, 760px);
      border: 2px solid #fff;
      padding: 40px 48px;
      text-align: center;
      background: #000;
    }

    .eyebrow,
    .prompt {
      font-size: 15px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .eyebrow {
      margin: 0 0 18px;
    }

    h1 {
      margin: 0;
      font-size: clamp(36px, 6vw, 60px);
      line-height: 1.1;
    }

    hr {
      margin: 22px 0;
      border: 0;
      border-top: 1px solid #fff;
    }

    p {
      margin: 0;
      font-size: 20px;
      line-height: 1.6;
    }

    .prompt {
      margin-top: 22px;
    }
  </style>
</head>
<body>
  <main>
    <p class="eyebrow">[ Lichess OAuth ]</p>
    <h1>ChessShock Setup Complete</h1>
    <hr>
    <p>Close this tab and return to the terminal.</p>
    <p class="prompt">&gt; ready</p>
  </main>
</body>
</html>
""".encode("utf-8")


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(72)


def _build_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
