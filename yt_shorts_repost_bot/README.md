# Full-Short repost bot

This separate package scans configured feeds, downloads each selected source in
full without enforcing the obsolete 60-second cutoff, prepares it in `copy` or
`render` mode and uploads to an explicitly connected destination account.
YouTube—not the bot—determines whether a longer upload is surfaced as a Short or
a regular video.

Only repost media you own or are licensed to reuse. See the repository-level
[README](../README.md) and [SECURITY](../SECURITY.md) documents before running.
Copy `.env.example` to ignored `.env`.

- `copy`: clean H.264/AAC re-encode, preserving the source appearance.
- `render`: optional captions, BGM, account text and vertical fitting.

Each named account has isolated project-relative OAuth paths, an exact
channel-ID/title safety lock, independent retry state, a DST-aware US time-zone
posting window and an atomically reserved rolling upload limit. Empty source
lists remain empty. Missing OAuth and explicit dry-run never create fake records.

Commands:

```bash
bash setup.sh
./run_bot.sh --mode status
./run_bot.sh --mode once --account "My Channel"
./run_bot.sh --mode scheduler
./run_bot.sh --mode process-url --account "My Channel" --url "https://www.youtube.com/shorts/..."
./run_bot.sh --mode webui
```
