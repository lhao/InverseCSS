# Inverse CSS

Code for **Inverse CSS**, a synthetic employer–employee population for Maryland, 2021–2024, built from published government tables (BLS QCEW, Census QWI and related sources).

Author: Lingxin Hao, Johns Hopkins University. Status: pilot. Version 1.0 is the code behind the Sage Research Methods case "Suppression Imputation and Joint Distributions With an AI Coding Agent: Building Synthetic Employer Data From Published Tables" (2026); the code was written with an AI coding agent (Claude Code, Anthropic) under the author's direction.

**This repository contains code only.** No data are included: public tables are cited by release and download date (see [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)), and IPUMS microdata cannot be redistributed.

## What the pipeline does
The imputed values are plausible draws consistent with the published tables. They are not the confidential values.

| Stage | Scripts | What it does |
|---|---|---|
| 0. Download | `fetch_qcew.py`, `fetch_qwi.py`, `fetch_bed.py`, `fetch_external.py`, `read_ipums.py` | Download public tables and record each file in a manifest |
| 1. Suppressed cells | `impute_qcew.py`, `convert_qcew_naics.py`, `impute_qwi.py`, `downscale_qwi.py` (runner: `stage1_md.sh`) | Multiple imputation (M = 20) of suppressed QCEW and QWI cells within published parent totals, raked to state totals; county × NAICS 6-digit × quarter groups |
| 1 checks | `--validate` options, `ablation_qcew.sh`, `ablation_summary.py`, `check_downscale.py`, `compare_qwi_qcew*.py` | Hide-and-check against published cells; the impute/rake ablation |
| 2. Calibration | `classify_breaks.py`, `event_rates.py`, `calibrate_*.py`, `priors.py` | Rates and priors for establishment events and composition |
| 3. Establishments and panel | `create_establishments.py`, `build_panel.py`, `validate_heldout.py`, `merge_versions.py`, `subset_panel.py` | Synthetic establishments, linked across quarters; held-out validation |
| 4. Workers | `worker_groups.py`, `worker_stockflow.py`, `plasmode_stage1.py`, `occupation_*.py`, `acs_worker_tables.py`, `cps_firmsize_tables.py`, `nativity.py` | Worker groups per establishment, stock–flow consistency, occupation and AI exposure |
| Analysis | `s3_model.py`, `build_extract.py` | Analysis models and extracts |
| HPC | `slurm/` | Slurm job scripts for the ARCH Skipjack cluster (see `slurm/README.md`) |

Shared handling of known data conditions is in `conditions.py`; the condition codes (A8, C1, ...) refer to the data-conditions register, [`docs/DATA_CONDITIONS.md`](docs/DATA_CONDITIONS.md).

## Running
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r code/requirements.txt
# put the downloaded tables under data/raw/ (layout in docs/data_manifest.csv), then for stage 1:
bash code/stage1_md.sh <output folder>
```
Run from the project root (the folder that holds `code/` and `data/`). Results are reproducible given the seeds in the scripts and the same input files (check them against `docs/data_manifest.csv`).

Reference environment for version 1.0: Python 3.11.5 with the packages in `code/requirements.txt` (NumPy 1.24.3). An independent rerun of three of the stage-1 checks on Windows with Python 3.14 reproduced their results; fewer than one in 100,000 imputed values differed, by one job, where a value fell exactly on a rounding tie.

API contact details are read from environment variables (for example `BLS_CONTACT_EMAIL`); never put keys or tokens in files.

## Citation and license
Citation: please cite the archived release (see [`CITATION.cff`](CITATION.cff)): Hao, L. (2026). *InverseCSS* (Version 1.0) [Computer software]. Zenodo. https://doi.org/10.5281/zenodo.24209077 (all versions: https://doi.org/10.5281/zenodo.24209076)
License: MIT for the code (see [`LICENSE`](LICENSE)). The data sources keep their own terms (see `docs/DATA_SOURCES.md`).
