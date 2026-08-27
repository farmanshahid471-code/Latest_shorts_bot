"""Tests for the local OAuth callback (fixes 'OAuth 2 MUST utilize https')."""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

import pytest
from oauthlib.oauth2.rfc6749.errors import InsecureTransportError
from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

from yt_shorts_bot import uploader as uploader_module
from yt_shorts_bot.uploader import YouTubeUploader


def test_oauthlib_rejects_plain_http_callback_but_accepts_https(monkeypatch):
    """
    Root cause: oauthlib >= 3.2 raises InsecureTransportError for ANY non-HTTPS
    callback URI - even the standard http://localhost loopback used by desktop
    OAuth. The bot must therefore present the callback as https://localhost.
    """
    http_callback = "http://localhost:8123/?code=abc&state=xyz"
    https_callback = "https://localhost:8123/?code=abc&state=xyz"

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    with pytest.raises(InsecureTransportError):
        parse_authorization_code_response(http_callback, state="xyz")

    parsed = parse_authorization_code_response(https_callback, state="xyz")
    assert parsed["code"] == "abc"
    assert parsed["state"] == "xyz"


def test_auth_flow_presents_loopback_callback_as_https(monkeypatch):
    """
    Full local callback round trip: the flow's loopback server receives a
    normal http://localhost redirect, and fetch_token must see it rewritten to
    https://localhost so oauthlib parses it (this is exactly what Google's own
    run_local_server does).
    """
    state = "test-state-123"
    recorded: dict = {}

    class FakeFlow:
        def __init__(self):
            self.redirect_uri = None
            self.credentials = object()

        def authorization_url(self, **kwargs):
            recorded["redirect_uri"] = self.redirect_uri
            return "https://accounts.google.com/o/oauth2/v2/auth?client_id=x", state

        def fetch_token(self, authorization_response=None):
            recorded["authorization_response"] = authorization_response
            # This is the same oauthlib call requests-oauthlib makes inside a
            # real flow.fetch_token; it must NOT raise InsecureTransportError.
            parse_authorization_code_response(authorization_response, state=state)
            raise RuntimeError("STOP_AFTER_FETCH")

    monkeypatch.setattr(uploader_module.webbrowser, "open", lambda url: True)
    errors: list[Exception] = []

    def run():
        try:
            result = YouTubeUploader._run_auth_flow(FakeFlow())
            recorded["returned"] = result
        except Exception as exc:  # expected: our STOP sentinel
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Wait for the loopback server to accept connections, then hit it with the
    # same redirect Google would send (plain http://localhost).
    deadline = time.monotonic() + 10
    deliverable = False
    while time.monotonic() < deadline and thread.is_alive():
        redirect_uri = recorded.get("redirect_uri")
        if redirect_uri:
            parsed = urlparse(redirect_uri)
            port = parsed.port
            callback = (
                f"http://localhost:{port}/"
                f"?code=auth-code-123&state={urllib.parse.quote(state)}"
            )
            try:
                with urllib.request.urlopen(callback, timeout=2) as response:
                    response.read()
                deliverable = True
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.05)
                continue
        time.sleep(0.05)

    thread.join(timeout=10)

    assert deliverable, "loopback server never became reachable"
    assert errors and "STOP_AFTER_FETCH" in str(errors[0])
    response = recorded["authorization_response"]
    assert response.startswith("https://localhost:")
    query = urlparse(response).query
    params = urllib.parse.parse_qs(query)
    assert params["code"] == ["auth-code-123"]
    assert params["state"] == [state]
