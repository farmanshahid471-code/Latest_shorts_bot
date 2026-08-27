from __future__ import annotations

import base64
import io
import json
import re

from yt_shorts_bot.models import StateDB
from yt_shorts_repost_bot import webui


def auth_header(username="admin", password="strong-password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def configure_temp_panel(monkeypatch, tmp_path, accounts):
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    monkeypatch.setattr(webui, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(webui, "ACCOUNTS", accounts)
    db = StateDB(tmp_path / "state.db")
    monkeypatch.setattr(webui, "StateDB", lambda: db)
    return accounts_file


def test_basic_auth_and_csrf_protect_mutations(monkeypatch, tmp_path):
    configure_temp_panel(monkeypatch, tmp_path, [])
    monkeypatch.setattr(webui, "WEBUI_USERNAME", "admin")
    monkeypatch.setattr(webui, "WEBUI_PASSWORD", "strong-password")
    app = webui.create_app(testing=False)
    client = app.test_client()

    assert client.get("/").status_code == 401
    page = client.get("/", headers=auth_header())
    assert page.status_code == 200
    match = re.search(r'name="_csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    token = match.group(1)

    assert client.post("/api/accounts/add", headers=auth_header()).status_code == 403
    response = client.post(
        "/api/accounts/add",
        data={"_csrf_token": token},
        headers=auth_header(),
    )
    assert response.status_code == 302
    saved = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]
    assert saved["client_secret"] == "accounts/new channel 1/client_secret.json"
    assert saved["token"] == "accounts/new channel 1/token.json"


def test_active_tab_is_submitted_explicitly(monkeypatch, tmp_path):
    accounts = [
        {"name": "First", "target_channels": [], "enabled": True},
        {"name": "Second", "target_channels": [], "enabled": True},
    ]
    configure_temp_panel(monkeypatch, tmp_path, accounts)
    app = webui.create_app(testing=True)
    client = app.test_client()
    html = client.get("/?account=Second").get_data(as_text=True)
    assert '<option value="Second" selected>' in html
    assert '<option value="">-- active tab account --</option>' not in html

    # The backend no longer silently substitutes the first account.
    response = client.post(
        "/api/process-url",
        data={"url": "https://www.youtube.com/shorts/abcdefghijk", "account": ""},
    )
    assert response.status_code == 302
    assert "Choose+the+destination+account" in response.headers["Location"]


def test_only_youtube_urls_are_accepted(monkeypatch, tmp_path):
    accounts = [{"name": "A", "target_channels": [], "enabled": True}]
    configure_temp_panel(monkeypatch, tmp_path, accounts)
    client = webui.create_app(testing=True).test_client()
    response = client.post(
        "/api/process-url",
        data={"url": "https://example.com/private", "account": "A"},
    )
    assert "Only+a+valid+YouTube+URL" in response.headers["Location"]


def test_path_like_account_name_cannot_write_credentials(monkeypatch, tmp_path):
    configure_temp_panel(monkeypatch, tmp_path, [])
    client = webui.create_app(testing=True).test_client()
    oauth = {
        "installed": {
            "client_id": "x",
            "client_secret": "y",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    response = client.post(
        "/api/client-secret",
        data={
            "account": "../../escape",
            "file": (io.BytesIO(json.dumps(oauth).encode()), "client_secret.json"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert "valid+account" in response.headers["Location"]
    assert not (tmp_path.parent / "escape").exists()


def test_public_bind_requires_password(monkeypatch):
    monkeypatch.setattr(webui, "WEBUI_PASSWORD", "")
    try:
        webui.run_webui(host="0.0.0.0", port=5999)
    except RuntimeError as exc:
        assert "WEBUI_PASSWORD" in str(exc)
    else:
        raise AssertionError("public unauthenticated bind should be refused")


def test_delete_account_also_clears_its_local_state(monkeypatch, tmp_path):
    accounts = [{"name": "A", "target_channels": [], "enabled": True}]
    configure_temp_panel(monkeypatch, tmp_path, accounts)
    database = StateDB(tmp_path / "state.db")
    database.record_video_state("video", status="UPLOADED_YOUTUBE", account="A")
    database.record_upload("video", "youtube-id", account="A")
    monkeypatch.setattr(webui, "StateDB", lambda: database)
    client = webui.create_app(testing=True).test_client()
    response = client.post("/api/accounts/delete", data={"account": "A"})
    assert response.status_code == 302
    assert database.get_video_state("video", "A") is None
    assert database.get_uploads_in_last_24_hours(account="A") == 0


def test_json_partial_update_does_not_reset_other_booleans(monkeypatch, tmp_path):
    accounts = [
        {
            "name": "A",
            "target_channels": [],
            "enabled": True,
            "watermark_enabled": True,
            "delete_after_upload": True,
        }
    ]
    accounts_file = configure_temp_panel(monkeypatch, tmp_path, accounts)
    client = webui.create_app(testing=True).test_client()
    client.post(
        "/api/account-settings/save",
        json={"account": "A", "title_prefix": "NEW"},
    )
    saved = json.loads(accounts_file.read_text())["accounts"][0]
    assert saved["watermark_enabled"] is True
    assert saved["delete_after_upload"] is True
