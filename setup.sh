#!/bin/bash
# ==============================================================================
# setup.sh - ONE-TIME setup for the YouTube Shorts bot (beginner friendly)
# Run this once:  bash setup.sh
# It installs ffmpeg, creates the Python environment, installs dependencies,
# and creates your .env file. Afterwards you only ever need: ./run_bot.sh ...
# ==============================================================================
set -e
cd "$(dirname "$0")"

echo "=============================================="
echo " STEP 1/4 - Checking FFmpeg (needed for video)"
echo "=============================================="
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  OK - ffmpeg is already installed"
else
    echo "  Installing ffmpeg (this may ask for your sudo password)..."
    sudo apt-get update -y
    sudo apt-get install -y ffmpeg
    echo "  OK - ffmpeg installed"
fi

echo ""
echo "=============================================="
echo " STEP 2/4 - Creating Python virtual environment"
echo "=============================================="
if [ -d ".venv" ]; then
    echo "  OK - .venv already exists"
else
    python3 -m venv .venv
    echo "  OK - created .venv"
fi

echo ""
echo "=============================================="
echo " STEP 3/4 - Installing Python dependencies"
echo "  (this downloads ~200MB and takes a few minutes)"
echo "=============================================="
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r yt_shorts_bot/requirements.txt
echo "  OK - dependencies installed"

echo ""
echo "=============================================="
echo " STEP 4/4 - Creating your .env config file"
echo "=============================================="
if [ -f "yt_shorts_bot/.env" ]; then
    echo "  OK - .env already exists (leaving it unchanged)"
else
    cp yt_shorts_bot/.env.example yt_shorts_bot/.env
    echo "  OK - created yt_shorts_bot/.env from template"
fi

echo ""
echo "=============================================="
echo " ✅ SETUP COMPLETE!"
echo "=============================================="
echo ""
echo " Next steps:"
echo "   1) Edit your settings:   nano yt_shorts_bot/.env"
echo "      (at minimum change TARGET_CHANNELS to YOUR channels)"
echo "   2) Test one video:       ./run_bot.sh --mode process-url --url \"https://www.youtube.com/watch?v=VIDEO_ID\""
echo "   3) Check status:         ./run_bot.sh --mode status"
echo "   4) Run 24/7:             ./run_bot.sh --mode scheduler"
echo ""
