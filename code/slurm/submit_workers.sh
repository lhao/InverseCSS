#!/bin/bash
# Submit the worker stages for one prior: arrays A (1 core) -> B (3 cores) -> C (2 cores), task by task (aftercorr),
# so each stage is billed only for the cores it needs (F13, ARCH job-efficiency rule: peak memory + 20%).
#   usage (Skipjack, project root):  bash code/slurm/submit_workers.sh RUN PANEL_RUN PRIOR VFROM VTO
#   e.g.  bash code/slurm/submit_workers.sh workers_m20 md_m20 random 6 20
# Optional: BEGIN=2026-11-01T00:15 holds array A (and so B, C) until then, e.g. until the monthly budget reset.
# Sizes (measured 2026-10-25 after the memory work; peak + margin): A 1 core / 3.9 GB (peak 2.5),
# B 2 cores / 7.8 GB (peak 4.6), C 1 core / 3.9 GB (peak 3.5; rerun a task with 2 cores if it runs out of memory).
set -euo pipefail
RUN=${1:?}; PANEL_RUN=${2:?}; PRIOR=${3:?}; VFROM=${4:?}; VTO=${5:?}; N=$(( (VTO - VFROM + 1) * 6 ))
export ICSS_ROOT=$PWD RUN PANEL_RUN PRIOR VFROM
mkdir -p logs runs/$RUN; S=code/slurm/workers_task.sbatch
a=$(sbatch --parsable --export=ALL,STAGE=A ${BEGIN:+--begin=$BEGIN} -J wA_$PRIOR --array=1-$N -c 1 --mem=3900M  --time=01:30:00 $S)
b=$(sbatch --parsable --export=ALL,STAGE=B -J wB_$PRIOR --array=1-$N -c 2 --mem=7800M  --time=01:30:00 --dependency=aftercorr:$a $S)
c=$(sbatch --parsable --export=ALL,STAGE=C -J wC_$PRIOR --array=1-$N -c 1 --mem=3900M  --time=01:30:00 --dependency=aftercorr:$b $S)
echo "$RUN $PRIOR (versions $VFROM-$VTO x 6 county groups): A $a -> B $b -> C $c" | tee -a runs/$RUN/jobs.txt
