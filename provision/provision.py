#!/usr/bin/env python3
"""Semi-automated provisioner: Google Cloud project + YouTube OAuth per channel.

What this tool DOES (scripted, repeatable):
  - creates one gcloud named configuration per account
  - signs you in per account via Google's own login page (2FA supported)
  - creates one Cloud project per account
  - enables YouTube Data API v3 on each project
  - validates the OAuth client JSON you download and installs it into the bot(s)
  - scaffolds/updates accounts.json entries for yt_shorts_bot / yt_shorts_repost_bot
  - runs the one-time OAuth consent per account, saves token.json
  - captures the connected channel name + id (the bot's destination safety lock)

What it deliberately does NOT do:
  - It NEVER handles Gmail passwords. Automated password logins violate Google's
    ToS, break on 2FA/CAPTCHA, and are the fastest way to get every account
    security-flagged at once. All authentication happens on Google's own pages.
  - It cannot click the two console-only steps (OAuth consent screen config and
    OAuth client creation) because Google exposes no public API for them.
    It prints exact deep links + a checklist instead (~2 min per account).

Quota math (why one project per account matters):
  default project quota = 10,000 units/day; one upload = 1,600 units
  => 6 uploads/day per account. Quota resets at midnight US-Pacific.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROVISION_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROVISION_DIR.parent
STATE_FILE = PROVISION_DIR / "state.json"
ACCOUNTS_LIST = PROVISION_DIR / "accounts.txt"
DOWNLOADS = PROVISION_DIR / "downloads"

BOTS: Dict[str, Path] = {
    "shorts": REPO_ROOT / "yt_shorts_bot",
    "repost": REPO_ROOT / "yt_shorts_repost_bot",
}

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

UPLOAD_COST_UNITS = 1600
DEFAULT_DAILY_QUOTA_UNITS = 10000

SLUG_RE = re.compile(r"[^a-z0-9-]+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}$")
GOOGLE_BLOCKED_PROJECT_WORDS = ("google", "ssl", "gcp")


# ---------------------------------------------------------------- utilities

def slugify(name: str) -> str:
    s = SLUG_RE.sub("-", name.strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:24].strip("-") or "channel"
    return s


def die(msg: str, code: int = 1) -> None:
    print(f"\n[ERROR] {msg}")
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def info(msg: str) -> None:
    print(msg)


def load_state() -> Dict[str, Dict[str, Any]]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # corrupt state should never brick the tool
            warn(f"state.json unreadable ({e}); starting a fresh state file")
    return {"accounts": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def state_entry(state: Dict[str, Any], name: str, email: str) -> Dict[str, Any]:
    accs = state.setdefault("accounts", {})
    ent = accs.get(name)
    if ent is None:
        ent = accs[name] = {"email": email}
    ent.setdefault("email", email)
    return ent


def parse_accounts_list(path: Path = ACCOUNTS_LIST) -> List[Tuple[str, str]]:
    if not path.exists():
        die(
            f"{path.name} not found. Copy accounts.txt.example to accounts.txt and "
            "list one account per line:  name = email@example.com"
        )
    out: List[Tuple[str, str]] = []
    seen = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        for sep in ("=", ",", "\t"):
            if sep in line:
                name, _, email = line.partition(sep)
                break
        else:
            die(f"{path.name} line {lineno}: expected 'name = email', got: {raw!r}")
        name, email = name.strip(), email.strip()
        if not name or not EMAIL_RE.match(email):
            die(f"{path.name} line {lineno}: bad entry {raw!r} (need 'name = email')")
        if name.lower() in seen:
            warn(f"duplicate name '{name}' on line {lineno}; keeping first")
            continue
        seen.add(name.lower())
        out.append((name, email))
    if not out:
        die(f"{path.name} has no accounts listed yet")
    return out


def pick_accounts(pattern: Optional[str], all_flag: bool) -> List[Tuple[str, str]]:
    accounts = parse_accounts_list()
    if all_flag or not pattern:
        return accounts
    p = pattern.strip().lower()
    matches = [(n, e) for n, e in accounts if p == n.lower() or p == slugify(n)]
    if not matches:
        die(f"no account named '{pattern}' in accounts.txt "
            f"(known: {', '.join(n for n, _ in accounts)})")
    return matches


def bot_dirs(bot: str) -> List[Tuple[str, Path]]:
    if bot == "both":
        return list(BOTS.items())
    if bot not in BOTS:
        die(f"unknown --bot value '{bot}' (use shorts | repost | both)")
    return [(bot, BOTS[bot])]


# ---------------------------------------------------------------- gcloud

def gcloud_exe() -> Optional[str]:
    return shutil.which("gcloud")


def run_gcloud(
    args: List[str],
    config: Optional[str] = None,
    capture: bool = True,
    check: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    exe = gcloud_exe()
    if not exe:
        die("gcloud CLI not found on PATH. Install the Google Cloud CLI:\n"
            "  https://cloud.google.com/sdk/docs/install\n"
            "then reopen this terminal and re-run.")
    cmd = [exe] + args
    # On Windows gcloud is a .cmd wrapper; subprocess cannot exec .cmd directly.
    if exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    env = dict(os.environ)
    if config:
        env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = config
    runner = (
        subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, check=False)
        if capture
        else subprocess.run(cmd, text=True, env=env, timeout=timeout, check=False)
    )
    return runner


def gcloud_account(config: str) -> str:
    proc = run_gcloud(["config", "get-value", "account"], config=config)
    return (proc.stdout or "").strip().lower()


def make_project_id(name: str) -> str:
    import secrets

    base = slugify(name)[:12].strip("-") or "chan"
    for _ in range(20):
        pid = f"ytsb-{base}-{secrets.token_hex(2)}"
        if PROJECT_ID_RE.match(pid) and not any(
            w in pid for w in GOOGLE_BLOCKED_PROJECT_WORDS
        ):
            return pid
    die("could not derive a valid project id")


# ---------------------------------------------------------------- command: doctor

def cmd_doctor(args: argparse.Namespace) -> None:
    ok = True
    exe = gcloud_exe()
    if exe:
        proc = run_gcloud(["--version"], timeout=30)
        first = (proc.stdout or "").strip().splitlines()
        info(f"[OK] gcloud found: {first[0] if first else 'version unknown'}")
    else:
        ok = False
        info("[MISSING] gcloud CLI - install: https://cloud.google.com/sdk/docs/install")
    v = sys.version_info
    if v >= (3, 9):
        info(f"[OK] Python {v.major}.{v.minor}")
    else:
        ok = False
        info(f"[BAD] Python {v.major}.{v.minor} - need 3.9+")
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        info("[OK] google auth/API libraries present (needed for 'connect')")
    except ImportError:
        warn("google OAuth libraries not installed yet - run the bot's setup "
             "(setup.bat / pip install -r requirements.txt) before 'connect'")
    accounts = parse_accounts_list() if ACCOUNTS_LIST.exists() else []
    info(f"[INFO] accounts.txt lists {len(accounts)} account(s)")
    for key, d in BOTS.items():
        info(f"[INFO] bot '{key}': {d} "
             f"({'accounts.json present' if (d / 'accounts.json').exists() else 'no accounts.json yet'})")
    if ACCOUNTS_LIST.exists():
        state = load_state()
        for name, email in accounts:
            st = state["accounts"].get(name, {})
            info(f"  - {name} <{email}> project={st.get('project_id', '-')}")
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------- command: init

def cmd_init(args: argparse.Namespace) -> None:
    state = load_state()
    targets = pick_accounts(args.account, args.all)
    info(f"Provisioning {len(targets)} account(s). Each one needs ONE browser "
         "sign-in on Google's own login page (2FA fine).\n")
    for name, email in targets:
        ent = state_entry(state, name, email)
        config = f"ytsb-{slugify(name)}"
        ent["config"] = config
        print("=" * 62)
        info(f"ACCOUNT: {name}  <{email}>")
        # 1) named gcloud configuration (isolates accounts from each other)
        run_gcloud(
            ["config", "configurations", "create", config, "--no-activate"],
            capture=True, check=False, timeout=60,
        )
        # 2) sign in (interactive). Skip if this config already holds the account.
        current = gcloud_account(config)
        if current == email.lower():
            info("  [skip] gcloud already signed in for this account")
        else:
            info("  A browser window opens -> sign in as THIS email:")
            info(f"        {email}")
            info("        (wrong account? close the tab and re-run this command)")
            proc = run_gcloud(["auth", "login"], config=config, capture=False, timeout=600)
            if proc.returncode != 0:
                warn(f"login failed/cancelled for {name}; skipping (re-run later)")
                continue
            current = gcloud_account(config)
            if current != email.lower():
                warn(
                    f"you signed in as '{current}' but this slot is '{email}'. Fix:\n"
                    f"  gcloud config configurations activate {config}\n"
                    f"  gcloud auth login\n"
                    f"then re-run init for '{name}'."
                )
                continue
        ent["gcloud_email"] = current
        # 3) project
        pid = ent.get("project_id")
        if pid and ent.get("project_created"):
            info(f"  [skip] project already created: {pid}")
        else:
            created = False
            for attempt in range(4):
                pid = make_project_id(name)
                info(f"  creating Cloud project {pid} ...")
                proc = run_gcloud(
                    ["projects", "create", pid, f"--name=Shorts Bot {name}"],
                    config=config, capture=True, check=False, timeout=120,
                )
                if proc.returncode == 0:
                    created = True
                    break
                err = (proc.stderr or "") + (proc.stdout or "")
                if "already exists" in err.lower():
                    continue  # random id collision; retry with a new id
                print(err.strip())
                warn(
                    "project creation refused. Fresh Gmail accounts sometimes need\n"
                    "  phone verification / a day or two before creating projects.\n"
                    f"  Or create it by hand: https://console.cloud.google.com/projectcreate\n"
                    f"  (create it as {email}), then: provision.py setproject {name} <project-id>"
                )
                break
            if not created:
                continue
            ent["project_id"] = pid
            ent["project_created"] = True
            save_state(state)
        run_gcloud(["config", "set", "project", pid], config=config, check=False, timeout=60)
        # 4) enable YouTube Data API v3
        if ent.get("api_enabled"):
            info("  [skip] YouTube Data API already enabled")
        else:
            info("  enabling YouTube Data API v3 (can take ~1 min) ...")
            proc = run_gcloud(
                ["services", "enable", "youtube.googleapis.com", "--project", pid],
                config=config, capture=True, check=False, timeout=300,
            )
            if proc.returncode != 0:
                print((proc.stderr or proc.stdout or "").strip())
                warn("API enable failed; re-run or enable by hand: "
                     f"https://console.cloud.google.com/apis/library/youtube.googleapis.com?project={pid}")
                continue
            ent["api_enabled"] = True
            save_state(state)
        info(f"  DONE cloud-side for {name}.")
        print_guide(state, name)


# ---------------------------------------------------------------- command: setproject

def cmd_setproject(args: argparse.Namespace) -> None:
    state = load_state()
    (name, email), = pick_accounts(args.account, False)
    pid = args.project_id.strip().lower()
    if not PROJECT_ID_RE.match(pid):
        die(f"'{pid}' is not a valid project id")
    ent = state_entry(state, name, email)
    ent["project_id"] = pid
    ent["project_created"] = True
    ent.setdefault("config", f"ytsb-{slugify(name)}")
    save_state(state)
    info(f"recorded project {pid} for {name}; enabling YouTube API ...")
    proc = run_gcloud(
        ["services", "enable", "youtube.googleapis.com", "--project", pid],
        config=ent["config"], capture=True, check=False, timeout=300,
    )
    if proc.returncode == 0:
        ent["api_enabled"] = True
        save_state(state)
        print_guide(state, name)
    else:
        print((proc.stderr or proc.stdout or "").strip())
        warn("enable failed; retry with: provision.py init " + name)


# ---------------------------------------------------------------- guide (manual console steps)

def print_guide(state: Dict[str, Any], name: str) -> None:
    ent = state["accounts"].get(name, {})
    pid = ent.get("project_id", "<project-id>")
    email = ent.get("email", "<email>")
    dl = DOWNLOADS / slugify(name) / "client_secret.json"
    print(
        f"""
------------------------------------------------------------
MANUAL STEPS for '{name}' (about 2 minutes, console-only - no API exists)
Console (make sure you are signed in as {email}):
  https://console.cloud.google.com/?project={pid}

1) OAuth consent screen (once per project):
   https://console.cloud.google.com/apis/credentials/consent?project={pid}
     - User type: External -> Create
     - App name: Shorts Bot {name}   User support email: {email}
       Developer contact: {email}  -> Save and Continue
     - Scopes -> Add: youtube.upload AND youtube.readonly -> Save
     - Test users -> Add: {email}  -> Save
     - IMPORTANT: Publish app -> Production (PUBLISH, confirm).
       Testing-mode tokens EXPIRE EVERY 7 DAYS; publishing keeps them alive.
       'Unverified app' warning on consent is expected and fine (<100 users).

2) Create the OAuth client:
   https://console.cloud.google.com/apis/credentials?project={pid}
     - + Create credentials -> OAuth client ID
     - Application type: Desktop app   Name: {slugify(name)}
     - Create -> Download JSON -> save it as:
       {dl}
       (the guide/verify step below will pick it up from there)

3) Then run:
       provision.bat verify {name}
       provision.bat scaffold {name}
       provision.bat connect {name}
------------------------------------------------------------
"""
    )


def cmd_guide(args: argparse.Namespace) -> None:
    state = load_state()
    for name, _ in pick_accounts(args.account, args.all):
        print_guide(state, name)


# ---------------------------------------------------------------- command: verify

def validate_client_secret(path: Path) -> List[str]:
    problems: List[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"not valid JSON: {e}"]
    if "installed" not in data:
        return ["missing 'installed' key - did you create a 'Desktop app' client? "
                "(Web/Android clients won't work with this bot)"]
    c = data["installed"]
    cid, csec = str(c.get("client_id") or ""), str(c.get("client_secret") or "")
    if not cid.endswith("apps.googleusercontent.com"):
        problems.append(f"client_id looks wrong: {cid!r}")
    if not csec:
        problems.append("client_secret is empty")
    return problems


def cmd_verify(args: argparse.Namespace) -> None:
    state = load_state()
    dirs = bot_dirs(args.bot)
    for name, email in pick_accounts(args.account, args.all):
        ent = state_entry(state, name, email)
        slug = slugify(name)
        src = DOWNLOADS / slug / "client_secret.json"
        if not src.exists():
            candidates = sorted(p for p in (DOWNLOADS / slug).glob("*.json")) if (DOWNLOADS / slug).is_dir() else []
            if len(candidates) == 1:
                src = candidates[0]
        if not src.exists():
            warn(f"{name}: no client_secret.json in provision/downloads/{slug}/ yet - "
                 "do the two console steps first (provision.bat guide " + name + ")")
            continue
        problems = validate_client_secret(src)
        if problems:
            warn(f"{name}: {src.name} failed validation:")
            for p in problems:
                print("        - " + p)
            continue
        for key, d in dirs:
            dest = d / "accounts" / slug / "client_secret.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            info(f"{name}: installed client secret -> {dest.relative_to(REPO_ROOT)}")
        ent["secret_verified"] = True
        ent["secret_installed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        info(f"{name}: client secret OK. Next: provision.bat scaffold {name}")


# ---------------------------------------------------------------- accounts.json helpers

ACCOUNT_ENTRY_DEFAULTS: Dict[str, Any] = {
    "client_secret": "",   # filled per account
    "token": "",           # filled per account
    "target_channels": [],
    "connected_channel": "",
    "connected_channel_id": "",
    "expected_channel": "",
    "max_daily_uploads": 6,
    "aspect": "9:16",
    "fill": "blur",
    "shorts_per_video": 1,
    "subtitles_enabled": False,
    "watermark": "",
    "watermark_enabled": False,
    "top_watermark": "",
    "top_watermark_enabled": False,
    "title_prefix": "",
    "title_hashtags": "",
    "selection_order": "newest",
    "min_minutes_between_uploads": 60,
    "posting_timezone": "America/Los_Angeles",
    "posting_start_time": "05:00",
    "posting_end_time": "17:00",
    "delete_after_upload": False,
    "delete_r2_after_upload": False,
    "enabled": False,
}


def load_accounts_json(bot_dir: Path) -> Dict[str, Any]:
    f = bot_dir / "accounts.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    example = bot_dir / "accounts.example.json"
    if example.exists():
        data = json.loads(example.read_text(encoding="utf-8"))
        data["accounts"] = []  # start clean; entries come from scaffold
        return data
    return {"_comment": "provisioned by provision/provision.py", "accounts": []}


def save_accounts_json(bot_dir: Path, data: Dict[str, Any]) -> None:
    (bot_dir / "accounts.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def upsert_entry(bot_dir: Path, name: str, updates: Dict[str, Any]) -> bool:
    """Merge updates into the named entry; returns True if file changed."""
    slug = slugify(name)
    data = load_accounts_json(bot_dir)
    accounts = data.setdefault("accounts", [])
    entry = next((a for a in accounts if str(a.get("name", "")).lower() == name.lower()), None)
    if entry is None:
        entry = dict(ACCOUNT_ENTRY_DEFAULTS)
        entry["name"] = name
        entry["client_secret"] = f"accounts/{slug}/client_secret.json"
        entry["token"] = f"accounts/{slug}/token.json"
        accounts.append(entry)
    entry.update(updates)
    save_accounts_json(bot_dir, data)
    return True


def cmd_scaffold(args: argparse.Namespace) -> None:
    state = load_state()
    dirs = bot_dirs(args.bot)
    for name, email in pick_accounts(args.account, args.all):
        slug = slugify(name)
        ent = state_entry(state, name, email)
        missing = [d for _, d in dirs if not (d / "accounts" / slug / "client_secret.json").exists()]
        if missing:
            warn(f"{name}: client secret not installed yet for all selected bots - "
                 f"run 'verify {name}' first; scaffolding anyway")
        for key, d in dirs:
            upsert_entry(d, name, {
                "client_secret": f"accounts/{slug}/client_secret.json",
                "token": f"accounts/{slug}/token.json",
            })
            info(f"{name}: accounts.json entry ready in {d.name} (enabled=false until connected)")
        ent["scaffolded"] = True
        save_state(state)
    info("\nNext: provision.bat connect <name>  (one browser sign-in; mints token.json)")


# ---------------------------------------------------------------- command: connect

def ensure_google_libs() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        die("google OAuth libraries missing. Run the bot's setup first "
            "(setup.bat / setup.sh, or: pip install -r requirements.txt)")


def cmd_connect(args: argparse.Namespace) -> None:
    ensure_google_libs()
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    state = load_state()
    dirs = bot_dirs(args.bot)
    for name, email in pick_accounts(args.account, args.all):
        slug = slugify(name)
        ent = state_entry(state, name, email)
        # authorize once, using the first installed secret; reuse for all bots
        secret = next(
            ((d / "accounts" / slug / "client_secret.json") for _, d in dirs
             if (d / "accounts" / slug / "client_secret.json").exists()),
            None,
        )
        if secret is None:
            warn(f"{name}: no client secret installed - run 'verify {name}' first")
            continue
        print("=" * 62)
        info(f"CONNECT: {name} <{email}>")
        info("  A browser window opens. Sign in as THAT email, then:")
        info("  Advanced -> Go to Shorts Bot (unsafe) -> Allow")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), YOUTUBE_SCOPES)
        try:
            # access_type=offline is the library default; prompt=consent forces
            # Google to always hand back a fresh refresh_token.
            creds = flow.run_local_server(
                port=0, authorization_url_params={"prompt": "consent"}
            )
        except Exception as e:
            warn(f"{name}: OAuth flow failed: {e}")
            if "invalid_scope" in str(e).lower() or "scope" in str(e).lower():
                warn("  -> consent screen is missing youtube.upload / youtube.readonly scopes")
            continue
        if not getattr(creds, "refresh_token", None):
            warn("no refresh_token returned; token would die when it expires. "
                 "Revoke at https://myaccount.google.com/permissions and re-run.")
        updates: Dict[str, Any] = {}
        try:
            service = build("youtube", "v3", credentials=creds, cache_discovery=False)
            resp = service.channels().list(part="id,snippet", mine=True).execute()
            items = resp.get("items") or []
        except Exception as e:
            warn(f"{name}: token saved, but channel lookup failed: {e}")
            items = []
        if items:
            title = items[0]["snippet"]["title"]
            chan_id = items[0]["id"]
            updates = {
                "connected_channel": title,
                "connected_channel_id": chan_id,
                "expected_channel": title,
            }
            ent["channel_title"] = title
            ent["channel_id"] = chan_id
            info(f"  connected channel: '{title}' ({chan_id})")
            uploads = DEFAULT_DAILY_QUOTA_UNITS // UPLOAD_COST_UNITS
            info(f"  quota: ~{uploads} uploads/day on this account "
                 "(resets midnight US-Pacific)")
        else:
            warn("no YouTube channel on this Google account yet - create one at "
                 "youtube.com ('Create a channel'), then re-run connect")
        for _, d in dirs:
            token = d / "accounts" / slug / "token.json"
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(creds.to_json(), encoding="utf-8")
            upsert_entry(d, name, updates)
            if updates and (d / "accounts.json").exists():
                data = load_accounts_json(d)
                e2 = next((a for a in data["accounts"]
                           if str(a.get("name", "")).lower() == name.lower()), None)
                if e2 is not None and e2.get("target_channels"):
                    e2["enabled"] = True
                    save_accounts_json(d, data)
            info(f"  token + entry written -> {d.name}")
        ent["token_ok"] = True
        ent["connected_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        if not updates:
            warn("channel lock not set; scheduler for this account stays disabled")
        elif any(
            (lambda e: e and e.get("target_channels") and e.get("enabled"))(
                next((a for a in load_accounts_json(d).get("accounts", [])
                      if str(a.get("name", "")).lower() == name.lower()), None)
            )
            for _, d in dirs
        ):
            info(f"  {name} is LIVE (enabled=true; quota ~"
                 f"{DEFAULT_DAILY_QUOTA_UNITS // UPLOAD_COST_UNITS} uploads/day)")
        else:
            info("  'enabled' stays false until you set target_channels "
                 "(source channels) for this account in accounts.json / the panel")


# ---------------------------------------------------------------- command: status

def cmd_status(args: argparse.Namespace) -> None:
    state = load_state()
    accounts = parse_accounts_list()
    print(f"{'name':<14}{'email':<30}{'project':<24}{'api':<5}"
          f"{'secret':<7}{'token':<6}{'channel':<22}")
    print("-" * 108)
    for name, email in accounts:
        ent = state["accounts"].get(name, {})
        slug = slugify(name)
        secret_ok = any(
            (d / "accounts" / slug / "client_secret.json").exists() for d in BOTS.values()
        ) or bool(ent.get("secret_verified"))
        token_ok = bool(ent.get("token_ok")) and any(
            (d / "accounts" / slug / "token.json").exists() for d in BOTS.values()
        )
        print(
            f"{name[:13]:<14}{email[:29]:<30}{ent.get('project_id', '-')[:23]:<24}"
            f"{'y' if ent.get('api_enabled') else '-':<5}"
            f"{'y' if secret_ok else '-':<7}"
            f"{'y' if token_ok else '-':<6}"
            f"{(ent.get('channel_title') or '-')[:21]:<22}"
        )
    if args.deep and gcloud_exe():
        info("\n[deep] gcloud configurations:")
        proc = run_gcloud(["config", "configurations", "list"], capture=True, timeout=60)
        print(proc.stdout or "")


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="provision.py",
        description="Semi-automated Google Cloud + YouTube OAuth provisioner "
                    "for the shorts bots. See provision/README.md.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="check prerequisites (gcloud, python, libs)")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("init", help="per account: gcloud login + project + enable API")
    p.add_argument("account", nargs="?", help="account name from accounts.txt")
    p.add_argument("--all", action="store_true", help="provision every listed account")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("setproject", help="record a manually created project id")
    p.add_argument("account", help="account name from accounts.txt")
    p.add_argument("project_id", help="existing project id created in the console")
    p.set_defaults(fn=cmd_setproject)

    p = sub.add_parser("guide", help="print console links + manual checklist")
    p.add_argument("account", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_guide)

    p = sub.add_parser("verify", help="validate + install downloaded client_secret.json")
    p.add_argument("account", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--bot", choices=["shorts", "repost", "both"], default="shorts")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("scaffold", help="create/refresh accounts.json entries")
    p.add_argument("account", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--bot", choices=["shorts", "repost", "both"], default="shorts")
    p.set_defaults(fn=cmd_scaffold)

    p = sub.add_parser("connect", help="one-time OAuth sign-in; mints token.json + channel lock")
    p.add_argument("account", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--bot", choices=["shorts", "repost", "both"], default="shorts")
    p.set_defaults(fn=cmd_connect)

    p = sub.add_parser("status", help="progress table for all accounts")
    p.add_argument("--deep", action="store_true", help="also list gcloud configurations")
    p.set_defaults(fn=cmd_status)

    return ap


def print_welcome() -> None:
    print(
        """
==============================================================
 SHORTS BOT - CHANNEL PROVISIONER (interactive mode)
==============================================================
You started me without a command, so here is a menu.
Type a command below and press Enter:

  doctor              check prerequisites (gcloud CLI, Python, libs)
  status              progress table for every account

Per account (replace <name> with one from accounts.txt):
  init <name>         sign in + create Cloud project + enable API
  guide <name>        console links for the 2 manual steps
  verify <name>       validate + install client_secret.json
  scaffold <name>     create the accounts.json entry
  connect <name>      one-time OAuth sign-in -> token + channel lock

First time? Start with:   doctor
Quit: type exit (or press Enter on an empty line)
Same commands also work in a normal terminal: provision.bat <command>
"""
    )


def run_cli(argv: List[str]) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


def main() -> None:
    if len(sys.argv) > 1:
        try:
            run_cli(sys.argv[1:])
        except KeyboardInterrupt:
            print("\nInterrupted - progress is saved in state.json; re-run any time.")
            sys.exit(130)
        return

    # No arguments (e.g. provision.bat was double-clicked): stay open,
    # show a menu, and accept commands until the user quits.
    import shlex

    print_welcome()
    while True:
        try:
            line = input("\nprovision> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line or line.lower() in ("q", "quit", "exit"):
            return
        try:
            run_cli(shlex.split(line))
        except SystemExit as e:
            if e.code not in (None, 0):
                info(f"(command exited with code {e.code} - see messages above)")
        except KeyboardInterrupt:
            info("(interrupted - progress is saved in state.json; re-run any time)")
        except Exception as e:  # never kill the menu on an unexpected error
            warn(f"unexpected error: {e}")
        info("(done - window stays open; type another command, or 'exit' to close)")


if __name__ == "__main__":
    main()
