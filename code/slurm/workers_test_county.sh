#!/bin/bash
# Step-1 measurement (2026-10-24): worker stages (groups, stock-flow, nativity, exposure) for ONE county x ONE version,
# both priors in parallel, from the merged M = 20 panels. Records time and peak memory of every stage.
#   usage (on Skipjack, from the project root, inside a job):  bash code/slurm/workers_test_county.sh RUN COUNTY VERSION
set -euo pipefail
RUN=${1:?}; CTY=${2:?}; V=${3:?}; R=runs/$RUN; O=runs/workers_test_${CTY}_v$V
ACS=data/processed/ipums/workplace_00006/acs_md_workplace_workers.parquet
one() {
  pr=$1; d=$O/$pr; mkdir -p $d
  T() { n=$1; shift; python3 code/slurm/timed.py $d/timing.txt $n $d/$n.log -- "$@"; }
  T subset     python3 code/subset_panel.py $R/p_$pr $d/panel --tag md --versions $V --counties $CTY
  T groups     python3 code/worker_groups.py $d/panel data/raw/qwi/R2026Q3 $ACS data/raw/external/lodes/md_wac_S000_JT02_2022.csv.gz $d/workers --prior $pr --draws 20 --tag md
  T stockflow  python3 code/worker_stockflow.py $d/workers $d/panel $d/workers --tag md
  T nativity   python3 code/nativity.py $d/workers $ACS data/processed/ipums/acs_00006_md_workplace.parquet data/processed/ipums/workplace_00006/cps_firmsize_sector.csv $d/workers --prior $pr --tag md
  T exposure   python3 code/occupation_exposure.py $d/workers $d/panel $ACS $d/exposure --prior $pr --tag md
  ls -la $d/workers $d/exposure >> $d/timing.txt
}
one random & one systematic & wait
cat $O/*/timing.txt
