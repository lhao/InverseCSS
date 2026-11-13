#!/bin/bash
# F13 check: building versions one at a time and merging gives the same data as building them together.
# Pilot slices, 2 versions, both priors. Run from the project root:  bash code/slurm/test_versions.sh OUTDIR
set -euo pipefail
OUT=${1:?output folder}
Q="data/raw/qcew data/processed/qcew_imputed data/processed/qcew_naics2022 data/raw/external/cbp data/raw/qwi/R2026Q3"
COMMON="$Q data/processed/qwi_downscale/md_n4_to_n6/qwi_downscaled_n6_M20_sn1_p3.csv.gz data/raw/external/bds/bds2023_st_sec_fa_md_2019-2023.csv.gz"
PANEL="--bed data/raw/external/bed --reclass-rates data/processed/reclass_calibration/reclass_rates.csv"
for pr in random systematic; do
  python3 code/create_establishments.py $Q "$OUT/all/e_$pr" --draws 2 --prior $pr > /dev/null 2>&1
  python3 code/build_panel.py $COMMON "$OUT/all/e_$pr/establishments_slices.csv.gz" "$OUT/all/p_$pr" --draws 2 --prior $pr $PANEL > /dev/null 2>&1
  for v in 1 2; do
    python3 code/create_establishments.py $Q "$OUT/v$v/e_$pr" --draws 2 --versions $v --prior $pr > /dev/null 2>&1
  done
  python3 code/merge_versions.py 3a "$OUT/merged/e_$pr" "$OUT/v1/e_$pr" "$OUT/v2/e_$pr" > /dev/null
  for v in 1 2; do
    python3 code/build_panel.py $COMMON "$OUT/merged/e_$pr/establishments_slices.csv.gz" "$OUT/v$v/p_$pr" --draws 2 --versions $v --prior $pr $PANEL > /dev/null 2>&1
  done
  python3 code/merge_versions.py 3b "$OUT/merged/p_$pr" "$OUT/v1/p_$pr" "$OUT/v2/p_$pr" > /dev/null
done
# compare: data files by decompressed content; reports without the run lines
for d in e_random e_systematic p_random p_systematic; do
  for f in "$OUT/all/$d"/*.csv.gz; do
    b=$(basename "$f")
    cmp -s <(gzip -dc "$f") <(gzip -dc "$OUT/merged/$d/$b") && echo "same    $d/$b" || echo "DIFFERS $d/$b"
  done
  r=$(ls "$OUT/all/$d"/report_*.txt | head -1); b=$(basename "$r")
  cmp -s <(grep -vE '^(run:|  part )' "$r") <(grep -vE '^(run:|  part )' "$OUT/merged/$d/$b") && echo "same    $d/$b (without run lines)" || echo "DIFFERS $d/$b"
done
echo "done $(hostname) $(date +%H:%M:%S)"
