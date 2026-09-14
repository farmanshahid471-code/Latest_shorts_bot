# Cross-posting to TikTok

Both bots can automatically post every finished Short to **TikTok** — on top of
YouTube. Cross-posting is:

- **Per account tab** — each channel opts in separately with its own
  TikTok credentials.
- **Independent of YouTube** — a YouTube quota wait, auth failure, or failed
  upload never blocks the TikTok post (and vice versa).
- **Official APIs only** — the TikTok Content Posting API. No username/password
  logins, so no ban risk from automation and no breakage from 2FA.
- **Idempotent** — every attempt is recorded in the local `social_posts`
  table, so a retry never publishes the same Short twice.
- **Dry-run aware** — `DRY_RUN=true` prepares everything but sends nothing.

Captions reuse the YouTube title + hashtags (TikTok titles are truncated to
150 characters, its API limit).

## 1. TikTok (Content Posting API)

TikTok needs a developer app + OAuth tokens. The bot uploads the video bytes
directly, so no public URL is required. Access tokens live only ~24 hours —
configure the refresh token + client key/secret and the bot renews them
automatically (new tokens are saved back to `accounts.json`).

### 1.1 Create the TikTok app

1. Go to <https://developers.tiktok.com/> → **Manage apps → Create app**.
2. Add the **Content Posting API** product (and Login Kit) and request these
   scopes: `user.info.basic`, `video.upload`, `video.publish`.
3. Set a **redirect URI** you control, e.g. `https://example.com/tiktok-callback`
   (localhost works for the one-time code capture).
4. Note the **Client key** and **Client secret**.

New apps start in **sandbox**: posts work but stay private until TikTok
approves the app (submit for audit in the developer portal when ready).

### 1.2 Authorize once (automatic — recommended)

Run the helper from the project folder:

```bash
python connect_tiktok.py
```

It asks for your client key/secret and the panel tab name, opens TikTok's
consent page, catches the redirect on a small local server, exchanges the code
and writes `open_id` + both tokens straight into `accounts.json`. Nothing to
copy by hand.

Your TikTok app must list this exact redirect URI (Login Kit → Redirect URI):

```
http://127.0.0.1:8787/tiktok-callback
```

Repeat once per TikTok account, using a different tab name each time. Use
`--bot repost` to set up the repost bot instead, and `--redirect-uri` if you
registered a different localhost port.

### 1.2b Authorize by hand (if you prefer)

1. Open this URL (fill in your values, URL-encode the redirect):
   `https://www.tiktok.com/v2/auth/authorize/?client_key=CLIENT_KEY&response_type=code&scope=user.info.basic,video.upload,video.publish&redirect_uri=REDIRECT_URI&state=xyz`
2. Log in with the TikTok account that should receive the posts and approve.
3. TikTok redirects to `REDIRECT_URI?code=...` — copy the `code`.
4. Exchange it (one `curl` — TikTok requires form-encoded parameters):
   ```bash
   curl --request POST "https://open.tiktokapis.com/v2/oauth/token/" \
     --header "Content-Type: application/x-www-form-urlencoded" \
     --data-urlencode "client_key=CLIENT_KEY" \
     --data-urlencode "client_secret=CLIENT_SECRET" \
     --data-urlencode "code=CODE" \
     --data-urlencode "grant_type=authorization_code" \
     --data-urlencode "redirect_uri=REDIRECT_URI"
   ```
   The response contains `access_token`, `refresh_token` (valid ~1 year),
   `open_id`, and `expires_in`.

### 1.3 Fill in the panel

Open the account tab → **📣 Cross-post to TikTok**:

- ✅ **Post to TikTok**
- **TikTok open_id**, **access token**, **refresh token** ← step 1.2
- **TikTok client key / secret** ← step 1.1 (enables auto-renewal)
- **TikTok privacy** — `Public to everyone` (default), friends, followers, or self.
- Press **Test TikTok** and watch the logs for `✅`.

## 2. Daily behavior & troubleshooting

- Cross-posting runs right after each Short is rendered, before temp-file
  cleanup — even when the YouTube upload for the same Short waits on quota.
- States per Short live in `social_posts` (`POSTED`, `FAILED`, `SKIPPED`,
  `DRY_RUN_READY`); `FAILED`/`SKIPPED` are retried on the next processing of
  that Short, `POSTED` never re-posts.
- Deleting an account tab also deletes its `social_posts` rows.

| Log message | Fix |
|---|---|
| `TikTok authorization failed` | Paste a fresh access token, or fill in client key/secret + refresh token for auto-renewal. |
| `TikTok privacy … not available` | The bot automatically falls back to an allowed level; adjust the panel setting to silence it. |
| `Token belongs to a different TikTok user` | The tokens came from another TikTok login — redo step 1.2 with the right account. |
| Posts stay private on TikTok | Normal for sandbox apps — submit the app for TikTok's audit. |

Never paste tokens into issues, commits, screenshots, or chat. `accounts.json`
is already Git-ignored; the panel never echoes stored secrets back.
