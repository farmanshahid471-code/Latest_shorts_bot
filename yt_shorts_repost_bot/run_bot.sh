#!/bin/bash
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/.."
[ -x .venv/bin/python ] || bash setup.sh
exec .venv/bin/python -m yt_shorts_repost_bot.main "$@"
