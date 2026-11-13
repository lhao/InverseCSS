"""Cut a subset of a step-3b panel (selected versions and counties) into its own folder (F13).

The worker-group scripts (worker_groups, worker_stockflow, nativity, occupation_exposure) read a whole panel
folder into memory. For statewide runs each Slurm job works on one subset: a version x county (or group of
counties). Random streams in step 4 are keyed by version, quarter, county and industry, so a subset gives
exactly the rows a full run would give for it.

Writes panel_<tag>.csv.gz, exits_<tag>.csv.gz and cells_<tag>.csv.gz with the same columns, keeping rows whose
version and county are selected.

Usage: python3 code/subset_panel.py <panel_dir> <out_dir> [--tag md] [--versions 1-2] [--counties 24031,24033]
"""
import argparse
import csv
import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('panel'); ap.add_argument('out'); ap.add_argument('--tag', default='md')
    ap.add_argument('--versions', default='', help="'' = all; 'A-B' or 'A'")
    ap.add_argument('--counties', default='', help="comma-separated county FIPS; '' = all")
    a = ap.parse_args()
    V = set(map(str, cond.parse_versions(a.versions, 20))) if a.versions else None
    C = {c.strip() for c in a.counties.split(',') if c.strip()} or None
    os.makedirs(a.out, exist_ok=True)
    for name in ('panel', 'exits', 'cells'):
        src = cond.require(os.path.join(a.panel, f'{name}_{a.tag}.csv.gz'))
        dst = os.path.join(a.out, f'{name}_{a.tag}.csv.gz')
        n = k = 0
        with cond.open_text(src) as fi, gzip.open(dst + '.part', 'wt', newline='') as fo:
            r = csv.reader(fi); w = csv.writer(fo); h = next(r); w.writerow(h)
            iv, ic = h.index('version'), h.index('county')
            for row in r:
                n += 1
                if (V is None or row[iv] in V) and (C is None or row[ic] in C):
                    w.writerow(row); k += 1
        os.replace(dst + '.part', dst)
        print(f'{name}: kept {k:,} of {n:,} rows')
    print(cond.run_line())


if __name__ == '__main__':
    main()
