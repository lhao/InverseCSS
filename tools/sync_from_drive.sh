#!/bin/bash
# Copy code (only) from the Drive master ~/InverseCSS into this repository.
# Usage: tools/sync_from_drive.sh   (run from anywhere). Never copies data/, drafts/, docs/word, caches.
set -euo pipefail
SRC="${ICSS_DRIVE:-$HOME/InverseCSS}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
rsync -a --delete \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
  --include '*/' --include '*.py' --include '*.sh' --include '*.sbatch' \
  --include 'requirements.txt' --include 'README.md' --exclude '*' \
  "$SRC/code/" "$REPO/code/"
# Data files must never enter the repo
if find "$REPO/code" -type f \( -name '*.csv' -o -name '*.gz' -o -name '*.parquet' -o -name '*.dat' -o -name '*.xlsx' -o -name '*.zip' \) | grep -q .; then
  echo "STOP: data-like file found in code/"; exit 1; fi
echo "synced code from $SRC"
python3 "$REPO/tools/make_public.py"
