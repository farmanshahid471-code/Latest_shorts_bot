# Cross-posting to Bilibili

Both bots can automatically submit every finished Short to **Bilibili**
(哔哩哔哩) — on top of YouTube and TikTok. Like the other destinations it is:

- **Per account tab** — each channel opts in separately with its own Bilibili
  credentials.
- **Independent of YouTube** — a YouTube quota wait, auth failure, or failed
  upload never blocks the Bilibili post (and vice versa).
- **Official API only** — the Bilibili Open Platform 视频稿件投递 endpoints. No
  cookie/password scraping, so no ban risk from automation.
- **Idempotent** — every attempt is recorded in the local `social_posts`
  table, so a retry never publishes the same Short twice.
- **Dry-run aware** — `DRY_RUN=true` prepares everything but sends nothing.

Important: a successful post means the archive was **accepted for review**.
Bilibili审核 usually takes minutes to a few hours, and the video is not public
until it passes. Non-verified members may submit at most **5 archives per day**.

## 1. Create the Open Platform app

1. Register at <https://open.bilibili.com/> (开放平台) and complete the
   developer onboarding (开发者入驻).
2. Create an app and request the video archive scopes. When authorizing, tick
   **all** permission items — at minimum 基础信息 and **UP主视频稿件管理**,
   otherwise submission returns `127007 应用无该接口权限`.
3. Note the **client_id** (also called Access Key) and the **app secret**.

## 2. Authorize once to get tokens

1. Send the user through the platform's OAuth authorize flow with your
   `client_id` and redirect URI, and copy the returned `code`.
2. Exchange it for tokens:
   ```bash
   curl --request POST "https://api.bilibili.com/x/account-oauth2/v1/token" \
     --header "Content-Type: application/json" \
     --data '{"client_id":"CLIENT_ID","client_secret":"APP_SECRET",
              "grant_type":"authorization_code","code":"CODE"}'
   ```
   The response contains `access_token`, `refresh_token` and `expires_in`
   (access tokens last ~30 days).

## 3. Fill in the panel

Open the account tab → **📣 Cross-post to TikTok & Bilibili**:

- ✅ **Post to Bilibili**
- **Bilibili client id / client secret** ← step 1
- **Bilibili access token / refresh token** ← step 2 (the refresh token lets
  the bot renew the access token by itself and save the new pair)
- **分区 tid** — the target category. `21` (日常) is the default; query the
  live list via `/arcopen/fn/archive/type/list` if you want another one.
- **Copyright** — `自制 / Original` or `转载 / Repost`. For 转载, also fill in
  the **repost source** field with the original video URL, as Bilibili
  requires attribution.
- Press **Test Bilibili** and watch the logs for `✅`.

## 4. How the upload works

The bot picks the flow based on file size, exactly as the API requires:

| Size | Flow |
|---|---|
| ≤ 100 MB | `video/init` with `utype=1`, then one `POST /video/v2/upload` |
| > 100 MB | `video/init` with `utype=0`, 8 MB chunks to `/video/v2/part/upload`, then `archive/video/complete` |

Afterwards the rendered thumbnail (when the pipeline produced one) is sent to
`archive/cover/upload`, and the archive is submitted with
`archive/add-by-utoken`. The returned `resource_id` (BV number) is stored in
`social_posts.remote_id`. Every `member.bilibili.com` call is signed with
HMAC-SHA256 over the sorted `x-bili-*` headers, as the platform mandates.

Titles are trimmed to 80 characters, descriptions to 250, and tags to a
200-character comma-separated string — Bilibili rejects anything longer.

## 5. Troubleshooting

| Log message | Fix |
|---|---|
| `Bilibili needs the rendered video file on disk` | Bilibili has no pull-from-URL flow; don't delete the temp file before cross-posting. |
| `Bilibili API error (4002)` / `(4008)` | Signature or MD5 mismatch — usually a wrong app secret. |
| `Bilibili API error (127007)` | The app lacks the archive scope, or the user did not tick all permissions when authorizing. |
| `Bilibili authorization failed (127001)` | Access token expired/revoked — paste a fresh one, or add the refresh token + client id/secret for auto-renewal. |
| `Bilibili API error (4003)` | The machine's clock is off by more than 10 minutes; sync NTP. |
| Archive never appears | Still in 审核; check the creator dashboard for a rejection reason. |

Never paste tokens into issues, commits, screenshots, or chat. `accounts.json`
is already Git-ignored; the panel never echoes stored secrets back.
