#!/usr/bin/env python3
"""One-time TikTok authorization helper.

Run this once per TikTok account. It opens TikTok's consent page, catches the
redirect on a tiny local web server, exchanges the code for tokens, and writes
them straight into ``accounts.json`` -- so you never copy/paste a token by hand.

    python connect_tiktok.py

The redirect URI it listens on must match the one registered in your TikTok app
exactly (default: http://127.0.0.1:8787/tiktok-callback).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USERINFO_URL = "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name"
SCOPES = "user.info.basic,video.upload,video.publish"
DEFAULT_REDIRECT = "http://127.0.0.1:8787/tiktok-callback"

_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>TikTok connected</title></head>
<body style="font-family:system-ui;background:#0d0d12;color:#eef0f5;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
  <div style="font-size:56px">{icon}</div>
  <h1 style="font-size:20px;margin:12px 0">{title}</h1>
  <p style="color:#9aa0ae">{body}</p>
</div></body></html>"""


class _Catcher(BaseHTTPRequestHandler):
    """Single-shot handler that captures ?code= from TikTok's redirect."""

    result: dict = {}
    expected_state = ""

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        error = (params.get("error_description") or params.get("error") or [""])[0]

        if error:
            _Catcher.result = {"error": error}
            page = _PAGE.format(icon="❌", title="Authorization failed", body=error)
        elif not code:
            # Ignore stray requests (favicon, etc.) without ending the wait.
            self.send_response(404)
            self.end_headers()
            return
        elif state != _Catcher.expected_state:
            # Mismatched state means the response is not the one we started.
            _Catcher.result = {"error": "state mismatch - possible CSRF; try again"}
            page = _PAGE.format(icon="❌", title="Authorization failed",
                                body="State mismatch. Please run the tool again.")
        else:
            _Catcher.result = {"code": code}
            page = _PAGE.format(icon="✅", title="TikTok connected",
                                body="You can close this tab and return to the terminal.")

        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the default stderr logging
        pass


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def authorize(client_key: str, redirect_uri: str, port: int) -> str:
    """Open the consent page and block until TikTok redirects back."""
    state = secrets.token_urlsafe(16)
    _Catcher.expected_state = state
    _Catcher.result = {}

    query = urllib.parse.urlencode({
        "client_key": client_key,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    url = f"{AUTH_URL}?{query}"

    server = HTTPServer(("127.0.0.1", port), _Catcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\nOpening TikTok in your browser. Log in as the account that should")
    print("receive the posts, then approve the permissions.\n")
    print(f"If nothing opens, paste this URL yourself:\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print(f"Waiting for the redirect on {redirect_uri} ...")
    try:
        while not _Catcher.result:
            thread.join(0.5)
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled.")
    finally:
        server.shutdown()

    if "error" in _Catcher.result:
        raise SystemExit(f"\nTikTok returned an error: {_Catcher.result['error']}")
    return _Catcher.result["code"]


def save_to_accounts(accounts_file: Path, account_name: str, values: dict) -> None:
    """Merge the tokens into the named account, creating it when needed."""
    if accounts_file.exists():
        payload = json.loads(accounts_file.read_text(encoding="utf-8"))
    else:
        payload = {"accounts": []}
    accounts = payload.setdefault("accounts", [])

    target = next(
        (a for a in accounts
         if str(a.get("name", "")).strip().lower() == account_name.strip().lower()),
        None,
    )
    if target is None:
        target = {"name": account_name, "target_channels": [], "enabled": True}
        accounts.append(target)
        print(f"\nCreated a new account entry '{account_name}'.")
    target.update(values)

    # Write via a temp file so an interrupted run cannot truncate accounts.json.
    temp = accounts_file.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(accounts_file)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Connect a TikTok account to the bot (one-time setup)."
    )
    parser.add_argument("--bot", choices=["clip", "repost"], default="clip",
                        help="Which bot's accounts.json to write to (default: clip)")
    parser.add_argument("--account", help="Account/tab name in the panel")
    parser.add_argument("--client-key")
    parser.add_argument("--client-secret")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT,
                        help=f"Must match your TikTok app exactly (default: {DEFAULT_REDIRECT})")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    package = "yt_shorts_bot" if args.bot == "clip" else "yt_shorts_repost_bot"
    accounts_file = root / package / "accounts.json"

    print("=" * 64)
    print("  TikTok account setup")
    print("=" * 64)

    client_key = args.client_key or input("\nClient key: ").strip()
    client_secret = args.client_secret or input("Client secret: ").strip()
    if not client_key or not client_secret:
        print("\nBoth the client key and secret are required.")
        return 1

    account_name = args.account or input(
        "Panel account/tab name (e.g. 'Channel 1'): ").strip()
    if not account_name:
        print("\nAn account name is required.")
        return 1

    redirect_uri = args.redirect_uri.strip()
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        print(f"\nThis helper can only catch redirects on localhost, not "
              f"'{parsed.hostname}'. Register {DEFAULT_REDIRECT} in your TikTok "
              f"app, or pass --redirect-uri.")
        return 1

    code = authorize(client_key, redirect_uri, port)
    print("\nGot the authorization code. Exchanging it for tokens...")

    try:
        tokens = _post_form(TOKEN_URL, {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
    except Exception as exc:
        print(f"\nToken exchange failed: {exc}")
        return 1

    if tokens.get("error"):
        print(f"\nTikTok rejected the exchange: "
              f"{tokens.get('error_description') or tokens['error']}")
        return 1

    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    open_id = str(tokens.get("open_id") or "").strip()
    if not access or not open_id:
        print(f"\nUnexpected response from TikTok: {tokens}")
        return 1

    display_name = ""
    try:
        info = _get_json(USERINFO_URL, access)
        display_name = str(
            ((info.get("data") or {}).get("user") or {}).get("display_name") or ""
        )
    except Exception:
        pass  # Purely cosmetic; never fail the setup over it.

    save_to_accounts(accounts_file, account_name, {
        "tiktok_enabled": True,
        "tiktok_open_id": open_id,
        "tiktok_access_token": access,
        "tiktok_refresh_token": refresh,
        "tiktok_client_key": client_key,
        "tiktok_client_secret": client_secret,
    })

    print("\n" + "=" * 64)
    print("  ✅ TikTok connected")
    print("=" * 64)
    if display_name:
        print(f"  Account      : {display_name}")
    print(f"  open_id      : {open_id[:12]}...")
    print(f"  Refresh token: {'saved' if refresh else 'MISSING - re-run to fix'}")
    print(f"  Written to   : {accounts_file}")
    print("\nNext: start the panel, open the TikTok tab and press 'Test TikTok'.")
    if not refresh:
        print("\nWithout a refresh token the access token dies in ~24h.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
