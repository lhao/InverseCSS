#!/bin/bash
# Worker stages for one (version, county group) of one prior; one array task each (F13). Submitted by submit_workers.sh.
# STAGE=A: subset + worker groups;  B: stock-flow;  C: nativity tables + exposure, then drop files that can be rebuilt.
# Array task id 1..(NV*6): version = VFROM + (id-1)/6, county group = (id-1)%6 + 1.
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --partition=med
#SBATCH --output=logs/%x_%A_%a.out
set -euo pipefail
module load python/3.11.9; source ~/envs/icss/bin/activate
cd "${ICSS_ROOT:?}"
ID=${SLURM_ARRAY_TASK_ID:?}; V=$(( ${VFROM:-1} + (ID - 1) / 6 )); G=$(( (ID - 1) % 6 + 1 ))
CGROUPS=( "" "24031" "24999" "24005,24027" "24033,24003" "24510,24021,24025,24013" \
         "24043,24017,24045,24047,24037,24015,24009,24041,24035,24001,24023,24019,24029,24011,24039" )
D=runs/${RUN:?}/${PRIOR:?}/v$V/g$G; mkdir -p $D
ACS=data/processed/ipums/workplace_00006/acs_md_workplace_workers.parquet
T() { n=$1; shift; python3 code/slurm/timed.py $D/timing.txt $n $D/$n.log -- "$@"; }
case "${STAGE:?}" in
  A) T subset  python3 code/subset_panel.py runs/${PANEL_RUN:?}/p_$PRIOR $D/panel --tag md --versions $V --counties ${CGROUPS[$G]}
     T groups  python3 code/worker_groups.py $D/panel data/raw/qwi/R2026Q3 $ACS data/raw/external/lodes/md_wac_S000_JT02_2022.csv.gz $D/workers --prior $PRIOR --draws 20 --tag md ;;
  B) T stockflow python3 code/worker_stockflow.py $D/workers $D/panel $D/workers --tag md ;;
  C) T nativity python3 code/nativity.py $D/workers $ACS data/processed/ipums/acs_00006_md_workplace.parquet data/processed/ipums/workplace_00006/cps_firmsize_sector.csv $D/workers --prior $PRIOR --tag md
     T exposure python3 code/occupation_exposure.py $D/workers $D/panel $ACS $D/exposure --prior $PRIOR --tag md
     # keep: workers_sf (worker rows + stocks), nativity tables, exposure, reports. Drop what can be rebuilt quickly.
     rm -f $D/workers/workers_md.csv.gz; rm -rf $D/panel ;;
esac
