"""OAuth loopback flow tests.

Current design: oauthlib >= 3.2 rejects ANY non-HTTPS callback (including the
standard loopback redirect http://localhost:PORT/) unless the global
OAUTHLIB_INSECURE_TRANSPORT env var is set. Instead of mutating global env, the
uploader presents the callback to oauthlib as https://localhost - nothing is
ever fetched from it; only the ?code/state params are parsed before the code
is exchanged with Google over the real HTTPS token endpoint.
"""
from __future__ import annotations

import os
import threading
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest

from yt_shorts_bot.uploader import YouTubeUploader as ClipUploader
from yt_shorts_repost_bot.uploader import YouTubeUploader as RepostUploader


LOOPBACK_CALLBACK = "http://localhost:54321/?code=oauth-code&state=oauth-state"


def _oauthlib_parse(uri: str, state: str = "oauth-state"):
    from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

    return parse_authorization_code_response(uri, state=state)


def test_oauthlib_rejects_plain_http_loopback(monkeypatch):
    """This is why the code rewrites the loopback callback to https."""
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    from oauthlib.oauth2.rfc6749.errors import InsecureTransportError

    with pytest.raises(InsecureTransportError):
        _oauthlib_parse(LOOPBACK_CALLBACK)


def test_rewritten_https_loopback_parses_without_env_hacks(monkeypatch):
    """The https-rewritten loopback URL parses cleanly with NO insecure env."""
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    parsed = _oauthlib_parse(LOOPBACK_CALLBACK.replace("http://", "https://"))
    assert parsed["code"] == "oauth-code"
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


class _FakeFlow:
    def __init__(self):
        self.redirect_uri = None
        self.credentials = object()
        self.authorization_response = None

    def authorization_url(self, **_kwargs):
        return "https://accounts.google.com/o/oauth2/auth?dummy=1", "state"

    def fetch_token(self, authorization_response=None):
        from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

        parse_authorization_code_response(authorization_response, state="xyz")
        self.authorization_response = authorization_response


@pytest.mark.parametrize(
    ("uploader_cls", "webbrowser_target"),
    [
        (ClipUploader, "yt_shorts_bot.uploader.webbrowser.open"),
        (RepostUploader, "yt_shorts_repost_bot.uploader.webbrowser.open"),
    ],
)
def test_run_auth_flow_rewrites_loopback_callback_to_https(
    monkeypatch, uploader_cls, webbrowser_target
):
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    flow = _FakeFlow()

    def open_and_callback(_url):
        def hit():
            parsed = urlparse(flow.redirect_uri)
            deadline = time.monotonic() + 10
            # The local OAuth server starts listening a beat after the browser
            # is (mock-)opened; retry until the callback is accepted.
            while time.monotonic() < deadline:
                try:
                    urlopen(
                        f"http://127.0.0.1:{parsed.port}/?code=abc&state=xyz",
                        timeout=2,
                    )
                    return
                except URLError:
                    time.sleep(0.05)

        threading.Thread(target=hit, daemon=True).start()

    monkeypatch.setattr(webbrowser_target, open_and_callback)
    credentials = uploader_cls._run_auth_flow(flow)
    assert credentials is flow.credentials
    # On the wire the callback is http://localhost:PORT, but oauthlib is given
    # the https presentation (which needs no insecure-transport allowance).
    assert flow.redirect_uri.startswith("http://localhost:")
    assert flow.authorization_response.startswith("https://localhost:")
    assert "code=abc" in flow.authorization_response
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ
