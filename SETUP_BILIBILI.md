# Cross-posting to Bilibili

Both bots can prepare every finished Short for **Bilibili** (哔哩哔哩) in one of
two modes, chosen per account in the panel:

| Mode | What happens | Needs |
|---|---|---|
| 📁 **Manual** (default) | The clip is saved to a folder with a matching `.txt` holding the title, description and tags. You upload it yourself. | Nothing |
| 🔌 **Automatic** | The bot submits the archive through the Open Platform API. | Enterprise certification |

## Manual mode (recommended for most people)

Bilibili only grants upload API access to **certified mainland-China
enterprises** — there is no individual developer path, and the application form
demands a 统一社会信用代码 and a Chinese business license. Manual mode skips all
of that: no account connection, no credentials, no ban risk.

Set **Mode → Manual** in the Bilibili tab and optionally point **Export folder**
somewhere convenient (relative paths sit next to `accounts.json`; the default is
`bilibili_manual/`). After each cycle you get, per clip:

```
bilibili_manual/
  My Channel/
    2026-09-13_Funny-cat-moment_abc123.mp4   <- upload this
    2026-09-13_Funny-cat-moment_abc123.txt   <- copy fields from this
    2026-09-13_Funny-cat-moment_abc123.jpg   <- cover, when one was rendered
```

The `.txt` is plain UTF-8 and opens fine in Notepad:

```
TITLE
Funny cat moment

DESCRIPTION
Funny cat moment #cats #funny

TAGS (comma separated)
cats, funny

分区 / CATEGORY (TID)
21

COPYRIGHT
转载 / Repost

REPOST SOURCE
https://youtube.com/watch?v=...
```

Notes:

- Per-platform settings still apply, so a Bilibili-only clip length, title
  prefix or description shows up in the exported file and its notes.
- Exports are **idempotent** — a re-run logs `ALREADY_POSTED` rather than
  writing the clip twice — and they never overwrite an existing file.
- The video is **copied**, so the pipeline's normal cleanup still runs.
- Declare **转载** with the source URL for reposted content. Misdeclaring
  originality is a far more common cause of strikes than automation.
- A fresh 非正式会员 account may submit only **5 archives/day**; pass the
  100-question quiz to become a 正式会员 and lift it.

## 🎙 Dubbing clips into Chinese

English audio rarely performs on Bilibili, so both bots can dub a clip before
it is exported or uploaded. Tick **Dub into Chinese** in the Bilibili tab.

How it works, per clip:

1. The clip is transcribed with faster-whisper (the bot already does this for
   subtitles, so the timed text is free).
2. Each subtitle line is translated to Chinese.
3. A Chinese TTS voice speaks each line, and each is placed **at its own
   timestamp**, so the dub stays in sync with the picture.
4. The dub is mixed over the original audio, which is ducked to a quiet bed
   (set **Keep original audio under dub** to `0` to replace it entirely).

Lines whose translation runs longer than their slot are sped up slightly (never
more than 1.6x, past which the voice stops sounding human) instead of being
allowed to overlap the next line.

### Voices

| Voice | Description |
|---|---|
| `zh-CN-XiaoxiaoNeural` | 晓晓 — female, warm (default) |
| `zh-CN-YunxiNeural` | 云希 — male, lively |
| `zh-CN-YunjianNeural` | 云健 — male, deep, sports style |
| `zh-CN-XiaoyiNeural` | 晓伊 — female, youthful |
| `zh-CN-YunyangNeural` | 云扬 — male, news anchor |
| `zh-CN-liaoning-XiaobeiNeural` | 晓北 — female, northeastern accent |
| `zh-TW-HsiaoChenNeural` | 曉臻 — female, Taiwanese Mandarin |
| `zh-HK-HiuMaanNeural` | 曉曼 — female, Cantonese |

### Notes

- Dubbing is **Bilibili-only** — YouTube and TikTok keep the original audio.
- It is **best-effort**: if TTS or translation fails, the clip is still posted
  with its original audio and a warning is logged. A missing dub never costs
  you the post.
- It needs a transcript. The clip bot transcribes automatically when dubbing is
  on; the repost bot transcribes on demand (it normally skips transcription).
- Requires `edge-tts` and `deep-translator` (both free, no API key) plus
  FFmpeg. Install with `pip install -r requirements.txt`.
- Machine translation is not perfect. For a channel you care about, skim the
  exported `.txt` and fix the title before uploading.

## Automatic mode (enterprise only)

Everything below applies only to this mode. Like the other destinations it is:

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
