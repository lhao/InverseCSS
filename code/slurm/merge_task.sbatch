#!/bin/bash
# Merge the per-version parts of one step (F13); runs after its array finished without errors.
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --partition=med
#SBATCH --cpus-per-task=4
#SBATCH --mem=15600M
#SBATCH --time=02:00:00
#SBATCH --output=logs/%x_%j.out
set -euo pipefail
module load python/3.11.9; source ~/envs/icss/bin/activate
cd "${ICSS_ROOT:?}"; R=runs/${RUN:?}; P=$([ "${STEP:?}" = 3a ] && echo e || echo p)
python3 code/merge_versions.py "$STEP" "$R/${P}_${PRIOR:?}" $(ls -d "$R/${P}_$PRIOR"/v* | sort -V)
