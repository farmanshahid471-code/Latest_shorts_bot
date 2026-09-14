"""The one-time TikTok authorization helper."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import connect_tiktok as helper


def _callback(state: str, port: int = 8799, code: str = "THECODE", extra: str = ""):
    """Play TikTok: hit the local redirect the way the browser would."""
    def _hit():
        time.sleep(0.3)
        url = f"http://127.0.0.1:{port}/tiktok-callback?code={code}&state={state}{extra}"
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass
    threading.Thread(target=_hit, daemon=True).start()


def test_authorize_requests_the_posting_scopes(monkeypatch):
    seen = {}

    def _open(url):
        seen["url"] = url
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        _callback(query["state"][0])
        return True

    monkeypatch.setattr(helper.webbrowser, "open", _open)
    code = helper.authorize("CK", "http://127.0.0.1:8799/tiktok-callback", 8799)

    assert code == "THECODE"
    query = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert query["client_key"] == ["CK"]
    assert query["response_type"] == ["code"]
    # Posting needs all three scopes; a missing one breaks uploads later.
    assert set(query["scope"][0].split(",")) == {
        "user.info.basic", "video.upload", "video.publish"}


def test_authorize_rejects_a_mismatched_state(monkeypatch):
    """A wrong state means the redirect is not ours -- refuse it."""
    def _open(url):
        _callback("not-the-right-state")
        return True

    monkeypatch.setattr(helper.webbrowser, "open", _open)
    with pytest.raises(SystemExit, match="state mismatch"):
        helper.authorize("CK", "http://127.0.0.1:8799/tiktok-callback", 8799)


def test_authorize_surfaces_a_denied_consent(monkeypatch):
    def _open(url):
        def _hit():
            time.sleep(0.3)
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:8799/tiktok-callback"
                    "?error=access_denied&error_description=User+denied",
                    timeout=5,
                ).read()
            except Exception:
                pass
        threading.Thread(target=_hit, daemon=True).start()
        return True

    monkeypatch.setattr(helper.webbrowser, "open", _open)
    with pytest.raises(SystemExit, match="User denied"):
        helper.authorize("CK", "http://127.0.0.1:8799/tiktok-callback", 8799)


# ---------------------------------------------------------------------------
# Writing accounts.json
# ---------------------------------------------------------------------------
TOKENS = {
    "tiktok_enabled": True,
    "tiktok_open_id": "oid",
    "tiktok_access_token": "act",
    "tiktok_refresh_token": "rft",
    "tiktok_client_key": "CK",
    "tiktok_client_secret": "CS",
}


def test_save_merges_into_the_existing_account(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": [
        {"name": "Channel 1", "target_channels": ["https://yt/@a"],
         "max_daily_uploads": 5, "title_prefix": "keep me"},
        {"name": "Channel 2", "tiktok_open_id": "other"},
    ]}), encoding="utf-8")

    helper.save_to_accounts(accounts, "Channel 1", TOKENS)
    saved = json.loads(accounts.read_text(encoding="utf-8"))["accounts"]

    # Tokens land on the right account...
    assert saved[0]["tiktok_open_id"] == "oid"
    assert saved[0]["tiktok_enabled"] is True
    # ...without disturbing that account's other settings...
    assert saved[0]["target_channels"] == ["https://yt/@a"]
    assert saved[0]["title_prefix"] == "keep me"
    assert saved[0]["max_daily_uploads"] == 5
    # ...or any other account.
    assert saved[1] == {"name": "Channel 2", "tiktok_open_id": "other"}


def test_save_is_case_insensitive_about_the_name(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": [{"name": "Channel 1"}]}),
                        encoding="utf-8")
    helper.save_to_accounts(accounts, "  channel 1  ", TOKENS)
    saved = json.loads(accounts.read_text(encoding="utf-8"))["accounts"]
    # Matched the existing tab instead of creating a duplicate.
    assert len(saved) == 1 and saved[0]["tiktok_open_id"] == "oid"


def test_save_creates_a_missing_account(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": []}), encoding="utf-8")
    helper.save_to_accounts(accounts, "Brand New", TOKENS)
    saved = json.loads(accounts.read_text(encoding="utf-8"))["accounts"]
    assert saved[0]["name"] == "Brand New"
    assert saved[0]["enabled"] is True
    assert saved[0]["tiktok_access_token"] == "act"


def test_save_handles_a_missing_file(tmp_path):
    accounts = tmp_path / "accounts.json"
    helper.save_to_accounts(accounts, "First", TOKENS)
    assert json.loads(accounts.read_text(encoding="utf-8"))["accounts"][0]["name"] == "First"


def test_save_leaves_no_temp_file_behind(tmp_path):
    accounts = tmp_path / "accounts.json"
    helper.save_to_accounts(accounts, "A", TOKENS)
    assert list(tmp_path.glob("*.tmp")) == []


def test_saved_keys_match_what_the_bot_reads(tmp_path):
    """The helper must write exactly the keys social.py looks up."""
    from yt_shorts_bot.social import is_enabled

    accounts = tmp_path / "accounts.json"
    helper.save_to_accounts(accounts, "A", TOKENS)
    account = json.loads(accounts.read_text(encoding="utf-8"))["accounts"][0]
    assert is_enabled(account, "tiktok")
    for key in ("tiktok_open_id", "tiktok_access_token", "tiktok_refresh_token",
                "tiktok_client_key", "tiktok_client_secret"):
        assert account.get(key), f"{key} missing"
