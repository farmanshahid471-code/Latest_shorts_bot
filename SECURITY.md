# Security and credential rotation

The repository no longer tracks `.env`, `accounts.json`, OAuth client files,
OAuth tokens, browser cookies, databases, logs, generated videos, or downloaded
FFmpeg binaries. Existing local copies remain usable but are ignored by Git.

## Required one-time rotation

Older Git history contained live credential material. Ignoring/removing a file
in a new commit does **not** invalidate copies in old commits. Before treating
this repository as private again:

1. In Google Cloud Console, revoke/delete every OAuth client whose
   `client_secret.json` appeared in this repository and create replacements.
2. Revoke the bot's Google account access/tokens and reconnect each channel with
   its replacement OAuth client.
3. Sign out/revoke the exported YouTube browser sessions and export fresh
   cookies only if yt-dlp requires them.
4. Rotate any Cloudflare R2 access key that was ever placed in `.env`.
5. Purge the files from repository history with a history-rewriting tool such as
   `git filter-repo`, then force-push the cleaned history. Coordinate this with
   every collaborator because all old clones must be discarded/re-cloned.
6. Enable GitHub secret scanning and review access/audit logs for the affected
   Google and Cloudflare accounts.

Never paste credentials into issues, commits, logs, screenshots, or chat.

## Web control panel

The panel binds to `127.0.0.1` by default. A public/non-loopback bind is refused
unless `WEBUI_PASSWORD` is set. When remote access is needed, use a strong
password and place the panel behind HTTPS (for example, an authenticated reverse
proxy or SSH tunnel). HTTP Basic credentials must never cross an unencrypted
public network.

All state-changing panel requests require a session CSRF token. Account-derived
filesystem paths are normalized to safe project-relative folders.
