#!/bin/bash
# Helper script to launch the YouTube Shorts 24/7 Automation Bot
# Usage:
#   ./run_bot.sh --mode scheduler    (Run 24/7 background daemon)
#   ./run_bot.sh --mode once         (Run a single channel scan & upload cycle)
#   ./run_bot.sh --mode status       (Inspect SQLite db & R2 storage usage)
#   ./run_bot.sh --mode process-url --url "<youtube_url>"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Creating virtual environment and installing dependencies..."
    python3 -m venv "$SCRIPT_DIR/.venv"
    "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/yt_shorts_bot/requirements.txt"
fi

exec "$VENV_PYTHON" -m yt_shorts_bot.main "$@"
