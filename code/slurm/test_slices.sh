#!/bin/bash
# Reproducibility test: pilot slices, 2 versions, both priors (steps 3a + 3b).
# Run from the project root; same command on the Mac and on Skipjack, then diff the reports.
#   usage: bash code/slurm/test_slices.sh OUTDIR
set -euo pipefail
OUT=${1:?output folder}
Q="data/raw/qcew data/processed/qcew_imputed data/processed/qcew_naics2022 data/raw/external/cbp data/raw/qwi/R2026Q3"
COMMON="$Q data/processed/qwi_downscale/md_n4_to_n6/qwi_downscaled_n6_M20_sn1_p3.csv.gz data/raw/external/bds/bds2023_st_sec_fa_md_2019-2023.csv.gz"
for pr in random systematic; do
  python3 code/create_establishments.py $Q "$OUT/e_$pr" --draws 2 --prior $pr > "$OUT/e_$pr.log" 2>&1
  python3 code/build_panel.py $COMMON "$OUT/e_$pr/establishments_slices.csv.gz" "$OUT/p_$pr" --draws 2 --prior $pr \
    --bed data/raw/external/bed --reclass-rates data/processed/reclass_calibration/reclass_rates.csv > "$OUT/p_$pr.log" 2>&1
done
echo "done $(hostname) $(date +%H:%M:%S)"
