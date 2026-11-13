#!/bin/bash
# Submit the full-Maryland chain for one prior: 3a array -> merge -> 3b array -> merge (F13).
# Each job starts only if the previous one finished without errors (afterok).
#   usage (on Skipjack, from the project root):  bash code/slurm/submit_md.sh RUN_NAME PRIOR DRAWS
#   e.g.  bash code/slurm/submit_md.sh md_m20 random 20
set -euo pipefail
RUN=${1:?run name}; PRIOR=${2:?random or systematic}; DRAWS=${3:?number of versions}
export ICSS_ROOT=$PWD RUN PRIOR DRAWS
mkdir -p logs "runs/$RUN"
S=code/slurm
a=$(sbatch --parsable --export=ALL,STEP=3a -J "3a_$PRIOR" --array=1-"$DRAWS" $S/version_task.sbatch)
ma=$(sbatch --parsable --export=ALL,STEP=3a -J "m3a_$PRIOR" --dependency=afterok:$a $S/merge_task.sbatch)
b=$(sbatch --parsable --export=ALL,STEP=3b -J "3b_$PRIOR" --array=1-"$DRAWS" --dependency=afterok:$ma $S/version_task.sbatch)
mb=$(sbatch --parsable --export=ALL,STEP=3b -J "m3b_$PRIOR" --dependency=afterok:$b $S/merge_task.sbatch)
echo "$RUN $PRIOR: 3a $a -> merge $ma -> 3b $b -> merge $mb" | tee -a "runs/$RUN/jobs.txt"
