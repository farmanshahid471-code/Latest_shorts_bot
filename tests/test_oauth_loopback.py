from __future__ import annotations

import os
import threading
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest

from yt_shorts_bot.uploader import (
    YouTubeUploader as ClipUploader,
    allow_loopback_oauth_transport as clip_allow,
)
from yt_shorts_repost_bot.uploader import (
    YouTubeUploader as RepostUploader,
    allow_loopback_oauth_transport as repost_allow,
)


LOOPBACK_CALLBACK = "http://localhost:54321/?code=oauth-code&state=oauth-state"


def _oauthlib_parse(uri: str, state: str = "oauth-state"):
    from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

    return parse_authorization_code_response(uri, state=state)


@pytest.mark.parametrize("allow", [clip_allow, repost_allow])
def test_loopback_http_callback_is_accepted_only_inside_helper(allow, monkeypatch):
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    from oauthlib.oauth2.rfc6749.errors import InsecureTransportError

    with pytest.raises(InsecureTransportError):
        _oauthlib_parse(LOOPBACK_CALLBACK)

    with allow():
        parsed = _oauthlib_parse(LOOPBACK_CALLBACK)
    assert parsed["code"] == "oauth-code"
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


@pytest.mark.parametrize("allow", [clip_allow, repost_allow])
def test_loopback_helper_restores_previous_env_value(allow, monkeypatch):
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "keep-me")
    with allow():
        assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"
    assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "keep-me"


class _FakeFlow:
    def __init__(self):
        self.redirect_uri = None
        self.credentials = object()
        self.authorization_response = None

    def authorization_url(self, **_kwargs):
        return "https://accounts.google.com/o/oauth2/auth?dummy=1", "state"

    def fetch_token(self, authorization_response=None):
        from oauthlib.oauth2.rfc6749.errors import InsecureTransportError
        from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

        try:
            parse_authorization_code_response(authorization_response, state="xyz")
        except InsecureTransportError as exc:
            raise RuntimeError(str(exc)) from exc
        self.authorization_response = authorization_response


@pytest.mark.parametrize(
    ("uploader_cls", "webbrowser_target"),
    [
        (ClipUploader, "yt_shorts_bot.uploader.webbrowser.open"),
        (RepostUploader, "yt_shorts_repost_bot.uploader.webbrowser.open"),
    ],
)
def test_run_auth_flow_accepts_http_localhost_callback(
    monkeypatch, uploader_cls, webbrowser_target
):
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    flow = _FakeFlow()

    def open_and_callback(_url):
        def hit():
            parsed = urlparse(flow.redirect_uri)
            urlopen(f"http://127.0.0.1:{parsed.port}/?code=abc&state=xyz")

        threading.Thread(target=hit, daemon=True).start()

    monkeypatch.setattr(webbrowser_target, open_and_callback)
    credentials = uploader_cls._run_auth_flow(flow)
    assert credentials is flow.credentials
    assert flow.authorization_response.startswith("http://localhost:")
    assert "code=abc" in flow.authorization_response
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ
