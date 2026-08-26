# Clip-farming bot

This package scans configured source channels, selects replay/energy-ranked
15–20 second windows, optionally transcribes them, renders vertical Shorts and
uploads them to explicitly connected destination accounts.

Use the repository-level [README](../README.md) for setup, security, account,
retry, metadata and CLI documentation. Copy `.env.example` to ignored `.env`.

Main modules:

- `config.py`: paths/settings/account loading
- `fetcher.py`: feeds, combined heatmap + voice-excitement ranking, section downloads
- `processor.py`: Whisper/SRT and FFmpeg rendering
- `models.py`: SQLite leases, retry states and atomic quotas
- `uploader.py`: OAuth, destination lock and real YouTube uploads
- `storage.py`: optional bounded R2 backup
- `scheduler.py`: reloadable interruptible cycle loop
- `webui.py`: authenticated/CSRF-protected local control panel

Only content with `UPLOADED_YOUTUBE` (or a completed multi-part record) is
terminal. R2-only, dry-run, authentication, quota and failure states retry.
Automatic cycles also honor each account's DST-aware US time-zone posting window.
