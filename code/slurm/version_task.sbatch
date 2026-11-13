#!/bin/bash
# One version of step 3a or 3b for full Maryland (F13). Submitted as an array by submit_md.sh; do not edit per run.
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --partition=med
#SBATCH --cpus-per-task=1
#SBATCH --mem=3900M
#SBATCH --time=02:30:00
#SBATCH --output=logs/%x_%A_%a.out
set -euo pipefail
module load python/3.11.9; source ~/envs/icss/bin/activate
cd "${ICSS_ROOT:?}"
K=${SLURM_ARRAY_TASK_ID:?}; R=runs/${RUN:?}
Q="data/raw/qcew data/processed/qcew_imputed data/processed/qcew_naics2022 data/raw/external/cbp data/raw/qwi/R2026Q3"
COMMON="$Q data/processed/qwi_downscale/md_n4_to_n6/qwi_downscaled_n6_M20_sn1_p3.csv.gz data/raw/external/bds/bds2023_st_sec_fa_md_2019-2023.csv.gz"
if [ "${STEP:?}" = 3a ]; then
  python3 code/create_establishments.py $Q "$R/e_${PRIOR:?}/v$K" --draws "${DRAWS:?}" --versions "$K" --only '' --tag md --prior "$PRIOR"
else
  python3 code/build_panel.py $COMMON "$R/e_$PRIOR/establishments_md.csv.gz" "$R/p_$PRIOR/v$K" --draws "$DRAWS" --versions "$K" \
    --only '' --tag md --prior "$PRIOR" --bed data/raw/external/bed --reclass-rates data/processed/reclass_calibration/reclass_rates.csv
fi
