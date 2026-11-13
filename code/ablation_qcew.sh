#!/bin/bash
# Ablation of the QCEW suppression imputation (drafts/ablation_note.md, approved by Hao 2026-11-05):
#   A impute only (--no-rake), B rake only (--point --draws 1), C joint (production), D hot-deck (from the C runs).
# Same hide-and-check settings as code/stage1_md.sh step 1b (--validate 0.7, seed 9<year><q>, alpha 2, --persistent), so
# every arm is scored on the same hidden cells. Writes under OUT (a local folder, not Drive: F8).
#   usage (project root):  bash code/ablation_qcew.sh OUT     e.g. bash code/ablation_qcew.sh ~/icss_ablation
set -euo pipefail
OUT=${1:?output folder, e.g. ~/icss_ablation}
t() { echo "$(date +%H:%M:%S) $*"; }
run_arm() {   # arm name, extra options
  local arm=$1; shift; mkdir -p "$OUT/$arm/logs"
  for y in 2021 2022 2023 2024; do for q in 1 2 3 4; do
    python3 -I code/impute_qcew.py data/raw/qcew $y $q "$OUT/$arm" --alpha 2 --seed 9$y$q --validate 0.7 --persistent "$@" \
      > "$OUT/$arm/logs/impute_qcew_validation_${y}q$q.log" 2>&1 &
  done; done; wait
  for y in 2021 2022 2023 2024; do for q in 1 2 3 4; do
    [ -s "$OUT/$arm/validation_${y}q${q}_alpha2.csv" ] || { echo "missing $arm $y q$q"; exit 1; }
  done; done
  t "arm $arm done"
}
t "ablation start $(hostname)"
run_arm C_joint --draws 20
run_arm A_impute_only --draws 20 --no-rake
run_arm B_rake_only --draws 1 --point
python3 -I code/ablation_summary.py "$OUT" > "$OUT/ablation_report.txt"
t "done: $OUT/ablation_report.txt"
