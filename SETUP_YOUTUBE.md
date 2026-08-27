# 🎥 YouTube API Setup — How to Connect Your Channels to the Bot

Do these steps **once per channel** (each channel gets its OWN Google account + its OWN
Google Cloud project — that's the cleanest way to run 5–10 uploads/day per channel without
everything fighting over one API quota).

> 🤖 **Provisioning many channels?** `provision/provision.bat` automates the
> scriptable parts (per-account project + API enablement, secret validation,
> `accounts.json` entries, OAuth token minting) and prints exact console links
> for the two clicks that can't be scripted. See `provision/README.md`.

Total time: ~15 minutes per channel. Example with 3 channels:

| Channel | Google account | Google Cloud project |
|---|---|---|
| channel 1 | `channel1@gmail.com` | `shorts-bot-1` |
| channel 2 | `channel2@gmail.com` | `shorts-bot-2` |
| channel 3 | `channel3@gmail.com` | `shorts-bot-3` |

> ⚠️ A YouTube channel can only belong to ONE Google account — so each of your channels
> must live in its own Google account anyway. Give each one its own Cloud project and
> its own `client_secret.json`; the bot's tabs keep them perfectly separate.

---

## Step 1 — Create the Google account (once per channel)

1. Go to **https://accounts.google.com/signup** → create e.g. `channel1@gmail.com`.
2. Log in with it and create/claim your YouTube channel for it
   (youtube.com → your avatar → "Create a channel").

## Step 2 — Create a Google Cloud project (in THAT account)

1. Log into **https://console.cloud.google.com/** with **that channel's Google account**.
2. Top-left, next to "Google Cloud", click the **project selector** → **New Project**.
3. Name it (e.g. `shorts-bot-1`), click **Create**.
4. Make sure the new project is selected in the top bar.

## Step 3 — Enable the YouTube Data API v3

1. Go to **APIs & Services → Library** (left menu).
2. Search **"YouTube Data API v3"** → click it → **Enable**.

## Step 4 — Create the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **External** → **Create**.
3. Fill in:
   - **App name**: anything (e.g. `Shorts Bot 1`)
   - **User support email**: your email
   - **Developer contact email**: your email
   - Leave everything else empty → **Save and Continue**.
4. **Scopes** screen: click **Add or remove scopes**, search **"YouTube Data API"**,
   tick these two, then **Update**:
   - `.../auth/youtube.upload`
   - `.../auth/youtube.readonly`
   → **Save and Continue**.
5. **Test users**: click **Add users**, add **this channel's own Google account email**
   (e.g. `channel1@gmail.com`). → **Save and Continue** → **Back to Dashboard**.
6. **Publish the app** (IMPORTANT): on the consent dashboard click **Publish app**
   → select **In production** → confirm.
   > **Do not stay in Testing mode.** Testing-mode refresh tokens **expire after
   > 7 days**, which would force you to re-authorize every channel weekly.
   > In production an *unverified* app shows an "unverified app" warning that you
   > bypass once via Advanced → continue — fine for up to 100 of your own users.
   > No payment info is needed either way.

## Step 5 — Create the OAuth client + download the key

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials** → **OAuth client ID**.
3. **Application type**: **Desktop app**.
4. Name it (e.g. `shorts-bot-1-desktop`) → **Create**.
5. Click **Download JSON** — this is your `client_secret.json` for channel 1.
   Keep it somewhere safe and repeat Steps 2–5 in the NEXT Google account for channel 2.

## Step 6 — Put each key in its tab (web panel)

1. Start the panel (`run_ui.bat`, or `./run_bot.sh --mode webui`).
2. Open the tab of that channel (e.g. the "Gaming" tab; add tabs with the **`+`** button).
3. In **🔑 Credentials** → **Upload client_secret.json** → pick channel 1's file.
   (For many channels, better: drop the files into
   `yt_shorts_bot\accounts\<name>\client_secret.json` per tab — see Step 7.)
4. Click **Connect / Test YouTube** — a browser tab opens with Google login.

## Step 7 — Authorize (this picks WHICH channel the tab posts to)

1. In the login page, **sign in with THAT channel's own Google account**
   (e.g. `channel1@gmail.com` — NOT your main email).
2. You'll see **"Google hasn't verified this app"** → **Advanced** →
   **Go to Shorts Bot (unsafe)** → **Continue** → **Allow**.
   > This warning is normal for unverified personal apps — you're only connecting
   > your own account. (If you also upload a `token.json`… no: the bot saves it itself.)
3. The bot saves the token to the safe project-relative
   `accounts/<name>/token.json` path and reads the destination channel title + ID.
4. The tab's dot turns **green**. The first successful connection also sets the
   destination safety lock. Every upload re-verifies that exact channel ID (or
   exact title when an ID is unavailable) and fails closed on verification errors.

Repeat Steps 1–7 in each Google account for each of your channels.

---

## ⚠️ Important things to know

- **Quota:** each Google account gets **10,000 API units/day** (~6–10 uploads). With one
  separate account per channel, **every channel gets its own quota** — that's how you reach
  5–10 uploads/day per channel across many channels.
- **Tokens expire** (testing-mode refresh tokens last ~7 days): if a tab stops uploading,
  just press **Connect** again and re-login (per-account settings are kept).
- **"OAuth 2 MUST utilize https" / `insecure_transport` error:** this was a bug in older
  versions of this bot. oauthlib rejects even the standard `http://localhost` loopback
  callback. The fix rewrites that callback to `https://localhost` (same as Google's own
  library) — the code exchange itself still happens over real HTTPS. Update the bot (or
  re-download the latest zip) so Connect works again.
- **Never share** `client_secret.json` or `token.json` — anyone with them can control the channel.
- **R2 (Cloudflare) is optional.** The bot uploads directly to YouTube; R2 is just a backup.
  Leave the R2 lines as placeholders and the bot still posts (badge: LIVE for YouTube).
- **Copyright:** only post content you own or have rights to — re-uploading others'
  content gets strikes/bans.
- **Full settings reference:** see `SETTINGS_GUIDE.md`.
