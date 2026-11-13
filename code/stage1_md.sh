#!/bin/bash
# Stage 1 (fill and reconcile) plus the calibrations that read its outputs, for Maryland 2021-2024, M = 20.
# Written 2026-10-31 for the Maryland redo (ANALYSIS_STRATEGY S4a). The commands and options are those of the production
# runs of 2026-10-11 to 10-18, recovered from the session records, except:
#   - impute_qcew: one seed for all quarters with --persistent (streams keyed by version x county x parent group, F4),
#     instead of --seed $y$q (one sequential stream per quarter).
# Writes everything under OUT (a local folder, not Drive: F8); data/raw is read from the project root.
#   usage (project root):  bash code/stage1_md.sh OUT
set -euo pipefail
OUT=${1:?output folder, e.g. ~/icss_redo/processed}
Q=data/raw/qwi/R2026Q3; CP=$OUT/qcew_naics2022; IMP=$OUT/qcew_imputed
mkdir -p "$OUT"/{qcew_imputed,qcew_naics2022,qwi_imputed,qwi_downscale,breaks,reclass_calibration,county_size_calibration,logs}
L=$OUT/logs
t() { echo "$(date +%H:%M:%S) $*"; }

t "1. impute_qcew, 16 quarters (in parallel)"
for y in 2021 2022 2023 2024; do for q in 1 2 3 4; do
  python3 -I code/impute_qcew.py data/raw/qcew $y $q "$IMP" --draws 20 --alpha 2 --persistent > "$L/impute_qcew_${y}q$q.log" 2>&1 &
done; done; wait
for y in 2021 2022 2023 2024; do for q in 1 2 3 4; do [ -s "$IMP/qcew_imputed_${y}q${q}_M20.csv.gz" ] || { echo "missing $y q$q"; exit 1; }; done; done

t "1b. hide-and-check validation of the QCEW imputation (same settings as 2026-10-11: --validate 0.7, seed 9yq)"
mkdir -p "$IMP/validation"
for y in 2021 2022 2023 2024; do for q in 1 2 3 4; do
  python3 -I code/impute_qcew.py data/raw/qcew $y $q "$IMP/validation" --draws 20 --alpha 2 --seed 9$y$q --validate 0.7 --persistent > "$L/impute_qcew_validation_${y}q$q.log" 2>&1 &
done; done; wait

t "2. convert_qcew_naics (NAICS 2017 -> 2022)"
python3 code/convert_qcew_naics.py data/raw/qcew "$IMP" data/raw/reference/naics/2017_to_2022_NAICS.xlsx "$CP" --draws 20 > "$L/convert_qcew_naics.log" 2>&1

t "3. impute_qwi: validation, then the imputation"
python3 -I code/impute_qwi.py $Q data/raw/qcew "$IMP" "$OUT/qwi_imputed" --draws 20 --validate 0.5 --earn-size-c 4 --qcew-complete "$CP" > "$L/impute_qwi_validation.log" 2>&1
python3 -I code/impute_qwi.py $Q data/raw/qcew "$IMP" "$OUT/qwi_imputed" --draws 20 --earn-size-c 4 --qcew-complete "$CP" > "$L/impute_qwi.log" 2>&1

t "4. downscale_qwi: validation one level up (3 -> 4 digit), then 4 -> 6 digit"
python3 -I code/downscale_qwi.py $Q/qwi_md_sa_f_gc_n3_op_u_2021-2024.csv.gz $Q/qwi_md_sa_f_gc_n4_op_u_2021-2024.csv.gz $Q/qwi_md_sa_f_gc_n4_op_u_2021-2024.csv.gz \
  data/raw/qcew "$IMP" 4 "$OUT/qwi_downscale/validation_n3_to_n4" --validate --sigma-net 1 --passes 3 --earn-size-c 4 --qcew-complete "$CP" > "$L/downscale_validation.log" 2>&1 &
python3 -I code/downscale_qwi.py $Q/qwi_md_sa_f_gc_n4_op_u_2021-2024.csv.gz $Q/qwi_md_sa_f_gs_n6_op_u_2021-2024.csv.gz $Q/qwi_md_sa_f_gs_n6_op_u_2021-2024.csv.gz \
  data/raw/qcew "$IMP" 6 "$OUT/qwi_downscale/md_n4_to_n6" --sigma-net 1 --passes 3 --seed 42 --earn-size-c 4 --qcew-complete "$CP" \
  --parent-imputed "$OUT/qwi_imputed/qwi_imputed_county_M20.csv.gz" > "$L/downscale_md.log" 2>&1
wait

t "5. calibrations that read stage-1 outputs: breaks -> reclassification rates; county firm-size transfer variances"
python3 code/classify_breaks.py data/raw/qcew "$CP" "$OUT/breaks" > "$L/classify_breaks.log" 2>&1
python3 code/event_rates.py "$OUT/breaks" > "$L/event_rates.log" 2>&1
python3 code/calibrate_reclass.py data/raw/qcew "$CP" data/raw/external/cbp "$OUT/breaks" "$OUT/reclass_calibration" --k 60 --seed 99 > "$L/calibrate_reclass.log" 2>&1
python3 code/calibrate_county_size.py data/raw/qcew "$IMP" "$CP" $Q "$OUT/county_size_calibration" > "$L/calibrate_county_size.log" 2>&1
# demo_tilt_calibration reads QWI only (calibrate_demo_tilts.py <qwi_dir>): unchanged, reused from data/processed.
t "done $(hostname)"
