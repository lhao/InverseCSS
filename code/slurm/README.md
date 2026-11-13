# Running InverseCSS on a Slurm cluster

These scripts run the pipeline on a Slurm cluster, one version (imputation draw) per array task, then merge the versions. They were used on the Johns Hopkins ARCH cluster; adapt the account, paths and module names to your site.

## Before you start
- **Account:** the `.sbatch` files contain `#SBATCH --account=YOUR_ACCOUNT`. Replace it with your allocation, or pass `--account=...` to `sbatch`.
- **Python:** the same versions as `code/requirements.txt` (the reference runs used Python 3.11). Create an environment and install the requirements on the cluster.
- **Inputs:** the downloaded tables under `data/raw/` (layout and SHA-256 hashes in `docs/data_manifest.csv`).
- **Memory:** request what the jobs use; on many clusters, asking for more memory than the per-core limit silently adds (billed) cores. Measured peaks: version tasks ≤ 2.4 GB on 1 core; merges ≤ 12 GB.

## Copy the code to the cluster
```
rsync -rt code <user>@<login-host>:<project-dir>/
```

## Submit full Maryland (on the cluster, from the project directory)
```
bash code/slurm/submit_md.sh md_m20 random 20
bash code/slurm/submit_md.sh md_m20 systematic 20
```
Each call submits four jobs: step 3a as an array (one task per version), a merge, step 3b as an array, and a merge. A job starts only when the previous one finished without errors.

Check with `squeue -u <user>`; afterwards `sacct -j <id> -o JobID,State,Elapsed,MaxRSS`. A failed task's log is `logs/<name>_<job>_<task>.out`.

The merged outputs are in `runs/<run>/e_<prior>/` and `p_<prior>/` (per-version parts in `v1/`, `v2/`, ...). The merged report recomputes everything over all versions and lists each part's run line.

## Copy results back
```
rsync -rt --exclude 'v[0-9]*/' <user>@<login-host>:<project-dir>/runs/md_m20 <local-dir>/
```
Compare checksums (`md5sum` on the cluster, `md5 -r` on a Mac) before deleting anything on scratch storage.

## Tests
- `code/slurm/test_slices.sh OUT`: the pilot slices, 2 versions, both priors. Run the same command locally and on the cluster and compare; data and reports should be identical apart from floating-point rounding (register F13).
- `code/slurm/test_versions.sh OUT`: builds versions one at a time, merges them, and checks that the result equals a single run.
