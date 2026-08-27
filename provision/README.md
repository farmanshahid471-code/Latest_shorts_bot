# Channel Provisioner — many accounts, minimal clicking

This tool provisions **one Google Cloud project + one OAuth client + one bot
credentials folder per YouTube channel**, which gives each channel its
own upload quota (default: **10,000 API units/day = 6 uploads/day**, resets at
midnight US-Pacific).

> **Tip:** double-clicking `provision.bat` opens an interactive menu — no
> commands memorized, window stays open. The same commands work from a
> terminal (`cmd` in the folder's address bar → `provision.bat doctor`).

It automates everything Google allows to be automated. Two steps physically
cannot be scripted — the **OAuth consent screen** and **OAuth client creation**
have no public API (Google's console is the only door) — so the tool prints
exact deep links and a click checklist for those (~2 min per account).

> **No passwords, ever.** This tool deliberately does not accept or store Gmail
> passwords. Automated password logins violate Google's ToS, break on 2FA and
> bot checks, and are the fastest way to get *all* the accounts flagged at once.
> Every sign-in happens on Google's own login/consent page in your browser —
> one per account — after which refresh tokens run the bot forever.

## Prerequisites (once)

1. **Python 3.9+** (already needed by the bot).
2. **Google Cloud CLI**: https://cloud.google.com/sdk/docs/install
   (installer, then reopen the terminal). Check with `provision.bat doctor`.
3. Bot dependencies installed (`setup.bat` once — needed for the `connect` step).

## The flow

```
  copy accounts.txt.example -> accounts.txt, list your accounts
  provision.bat doctor              check gcloud/python/libs
  provision.bat init gaming         sign in once + project + API (scripted)
  provision.bat guide gaming        console links for the 2 manual steps
  provision.bat verify gaming       validates + installs client_secret.json
  provision.bat scaffold gaming     creates the accounts.json entry
  provision.bat connect gaming      sign in once -> token.json + channel lock
  provision.bat status              see where every account stands
```

Add `--all` to run any step across every listed account (e.g. `init --all`).
`--bot shorts|repost|both` (default `shorts`) picks which bot gets the
credentials — use `both` if a channel is used by `yt_shorts_bot` and
`yt_shorts_repost_bot`.

### What each step does

| Step | Scripted? | What happens |
|---|---|---|
| `init` | yes | creates an isolated `gcloud` config per account, opens Google's login (sign in as **that channel's** email — 2FA fine), creates project `ytsb-<name>-xxxx`, enables YouTube Data API v3 |
| `guide` | n/a | prints the two console deep links + checklist: consent screen (scopes `youtube.upload`, `youtube.readonly`, test user = own email, **publish to production**) and OAuth client (**Desktop app**) → download JSON into `provision\downloads\<name>\client_secret.json` |
| `verify` | yes | validates the JSON (desktop client, sane client_id/secret) and copies it to `yt_shorts_bot\accounts\<name>\client_secret.json` |
| `scaffold` | yes | merges a ready entry into `accounts.json` (`enabled: false` until connected) |
| `connect` | yes | runs the bot's OAuth flow (one browser sign-in), saves `token.json`, looks up the channel, fills `connected_channel` / `connected_channel_id` / `expected_channel` (the bot's mandatory destination safety lock) |

Interrupted runs are safe — progress lives in `provision/state.json`; every
step is idempotent and can be re-run.

If a **fresh Gmail account** is refused at project creation: that's Google's
anti-abuse friction, not a bug. Phone-verify the account, wait 24–48h, then
either re-run `init` or create the project manually at
https://console.cloud.google.com/projectcreate while signed in as that email
and register it with `provision.bat setproject <name> <project-id>`.

## Advice for provisioning many accounts (16–50)

- **Batch it**: ~5 new accounts per day rather than 40 in one sitting. Same-IP
  bursts of project creation + OAuth consents are a classic abuse signal.
- **One project per account, never shared** — a shared project means shared
  quota, shared audit trail, and one strike kills every channel using it.
- Keep the number of **test users** to the channel's own email only, and don't
  request more scopes than `youtube.upload` + `youtube.readonly`.
- **Publish each consent app to production** (the guide step includes it).
  Testing-mode refresh tokens **expire every 7 days**; production ones persist.
  The "unverified app" warning on the consent screen is expected and safe for
  your own accounts (Google allows it for up to 100 users).
- 6 uploads/day/channel is the default quota. The only sanctioned way higher is
  YouTube's official quota-extension audit — mass-project quota schemes violate
  the YouTube API Developer Policies and risk every linked channel.
- Consumer Google accounts cap at roughly 30 projects lifetime — irrelevant at
  one project per account, but don't park extra projects in them.

## Files

- `accounts.txt` — your account list (**never commit it; gitignored**)
- `state.json` — provisioning progress (**gitignored**)
- `downloads/<name>/client_secret.json` — where you save the console download
- everything under `yt_shorts_bot/accounts/<name>/` (secrets, **gitignored**)
