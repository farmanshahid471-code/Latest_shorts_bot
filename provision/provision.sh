#!/usr/bin/env bash
# Semi-automated Google Cloud + YouTube OAuth provisioner (macOS/Linux wrapper).
set -euo pipefail
cd "$(dirname "$0")"
python3 provision.py "$@"
