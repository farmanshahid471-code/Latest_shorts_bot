#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "[1/4] Checking FFmpeg..."
if ! command -v ffmpeg >/dev/null 2>&1; then
  sudo apt-get update -y && sudo apt-get install -y ffmpeg
fi
echo "[2/4] Creating venv..."
[ -d .venv ] || python3 -m venv .venv
echo "[3/4] Installing packages..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "[4/4] Creating .env..."
[ -f .env ] || cp .env.example .env
echo "✅ Setup complete. Run:  ./run_bot.sh --mode once"
