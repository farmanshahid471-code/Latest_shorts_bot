# Cross-posting to Instagram & TikTok

Both bots can automatically post every finished Short to **Instagram Reels**
and **TikTok** — on top of YouTube. Cross-posting is:

- **Per account tab** — each channel opts in separately with its own
  Instagram/TikTok credentials.
- **Independent of YouTube** — a YouTube quota wait, auth failure, or failed
  upload never blocks the Instagram/TikTok post (and vice versa).
- **Official APIs only** — Meta Graph API for Reels, TikTok Content Posting
  API for TikTok. No username/password logins, so no ban risk from automation
  and no breakage from 2FA.
- **Idempotent** — every attempt is recorded in the local `social_posts`
  table, so a retry never publishes the same Short twice to one platform.
- **Dry-run aware** — `DRY_RUN=true` prepares everything but sends nothing.

Captions reuse the YouTube title + hashtags (TikTok titles are truncated to
150 characters, its API limit).

## 1. Instagram Reels (Meta Graph API)

Requirements from Meta: an **Instagram Professional account** (Business or
Creator) **linked to a Facebook Page**, plus a Meta app token with publishing
permission. The video must be downloadable from a **public https URL**, so
Instagram also needs the R2 backup + `R2_PUBLIC_BASE_URL` (step 4).

### 1.1 Prepare the Instagram account

1. In the Instagram app: **Settings → Account type and tools → Switch to
   professional account** (Creator is fine).
2. Link a Facebook Page: **Settings → Business tools → Facebook Page → link
   or create one**. (A Page you manage; it can be unpublished.)

### 1.2 Create the Meta app

1. Go to <https://developers.facebook.com/apps> → **Create App** → choose the
   **Business** type.
2. Open the **Graph API Explorer** (<https://developers.facebook.com/tools/explorer>),
   select your app, and generate a **User access token** with these permissions:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement`. (For a personal bot you can stay in Development
   mode and add yourself as a test user; for other people's accounts the app
   needs App Review.)
3. Exchange it for a long-lived (~60-day) token by opening this URL in the
   browser (fill in your values):
   `https://graph.facebook.com/v25.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN`
4. Recommended: turn it into a **Page token** (effectively non-expiring):
   - `GET /me/accounts` → find your Page's `access_token`.
   - Verify at <https://developers.facebook.com/tools/debug/accesstoken/>.

### 1.3 Find the Instagram user ID

In the Graph API Explorer (or browser) with your token:

`GET /{page-id}?fields=instagram_business_account` → the returned `id` is the
**Instagram user ID** the panel asks for.

### 1.4 Expose the R2 bucket publicly

Meta's servers fetch the video file themselves, so the R2 backup must be
reachable at a public URL:

1. Configure the R2 backup in `.env` as usual (`R2_ACCOUNT_ID`,
   `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`).
2. In the Cloudflare dashboard: **R2 → your bucket → Settings → Public
   Access** → connect a **custom domain** (recommended) or enable the
   `r2.dev` public URL if your bucket still offers one.
3. Set in the bot's `.env`:
   `R2_PUBLIC_BASE_URL="https://clips.example.com"`

### 1.5 Fill in the panel

Open the account tab → **📣 Cross-post to Instagram & TikTok**:

- ✅ **Post to Instagram Reels**
- **Instagram user ID** ← step 1.3
- **Instagram access token** ← step 1.2 (Page token recommended)
- **Meta app ID / secret** (optional) — enables automatic refresh of
  expiring user tokens.
- Press **Test Instagram** and watch the logs for `✅`.

Limits to know: ~25 API-published Reels per account per day; clips must be
vertical MP4 (9:16 recommended — set the account's aspect accordingly) and
under ~90 seconds (some accounts 60 s). Long reposts are rejected by Meta and
logged as failed — YouTube is unaffected.

## 2. TikTok (Content Posting API)

TikTok needs a developer app + OAuth tokens. The bot uploads the video bytes
directly, so no public URL is required. Access tokens live only ~24 hours —
configure the refresh token + client key/secret and the bot renews them
automatically (new tokens are saved back to `accounts.json`).

### 2.1 Create the TikTok app

1. Go to <https://developers.tiktok.com/> → **Manage apps → Create app**.
2. Add the **Content Posting API** product (and Login Kit) and request these
   scopes: `user.info.basic`, `video.upload`, `video.publish`.
3. Set a **redirect URI** you control, e.g. `https://example.com/tiktok-callback`
   (localhost works for the one-time code capture).
4. Note the **Client key** and **Client secret**.

New apps start in **sandbox**: posts work but stay private until TikTok
approves the app (submit for audit in the developer portal when ready).

### 2.2 Authorize once to get tokens

1. Open this URL (fill in your values, URL-encode the redirect):
   `https://www.tiktok.com/v2/auth/authorize/?client_key=CLIENT_KEY&response_type=code&scope=user.info.basic,video.upload,video.publish&redirect_uri=REDIRECT_URI&state=xyz`
2. Log in with the TikTok account that should receive the posts and approve.
3. TikTok redirects to `REDIRECT_URI?code=...` — copy the `code`.
4. Exchange it (one `curl`):
   ```bash
   curl -X POST "https://open.tiktokapis.com/v2/oauth/token/" \
     -H "Content-Type: application/json" \
     -d '{"client_key":"CLIENT_KEY","client_secret":"CLIENT_SECRET",
          "code":"CODE","grant_type":"authorization_code",
          "redirect_uri":"REDIRECT_URI"}'
   ```
   The response contains `access_token`, `refresh_token` (valid ~1 year),
   `open_id`, and `expires_in`.

### 2.3 Fill in the panel

Open the account tab → **📣 Cross-post to Instagram & TikTok**:

- ✅ **Post to TikTok**
- **TikTok open_id**, **access token**, **refresh token** ← step 2.2
- **TikTok client key / secret** ← step 2.1 (enables auto-renewal)
- **TikTok privacy** — `Public to everyone` (default), friends, followers, or self.
- Press **Test TikTok** and watch the logs for `✅`.

## 3. Daily behavior & troubleshooting

- Cross-posting runs right after each Short is rendered, before temp-file
  cleanup — even when the YouTube upload for the same Short waits on quota.
- States per Short live in `social_posts` (`POSTED`, `FAILED`, `SKIPPED`,
  `DRY_RUN_READY`); `FAILED`/`SKIPPED` are retried on the next processing of
  that Short, `POSTED` never re-posts.
- Deleting an account tab also deletes its `social_posts` rows.

| Log message | Fix |
|---|---|
| `Instagram needs a public video URL` | Enable the R2 backup and set `R2_PUBLIC_BASE_URL`. |
| `Meta API error … permission` / `(#10)` | The token lacks `instagram_content_publish`, or the IG account is not Professional / not linked to the Page. |
| `Meta could not process the video` | Wrong format or too long; use 9:16 MP4 under ~90 s. |
| `Instagram token expired` | Paste a fresh token (or a Page token), or fill in app id/secret for auto-refresh. |
| `TikTok authorization failed` | Paste a fresh access token, or fill in client key/secret + refresh token for auto-renewal. |
| `TikTok privacy … not available` | The bot automatically falls back to an allowed level; adjust the panel setting to silence it. |
| `Token belongs to a different TikTok user` | The tokens came from another TikTok login — redo step 2.2 with the right account. |
| Posts stay private on TikTok | Normal for sandbox apps — submit the app for TikTok's audit. |

Never paste tokens into issues, commits, screenshots, or chat. `accounts.json`
is already Git-ignored; the panel never echoes stored secrets back.
