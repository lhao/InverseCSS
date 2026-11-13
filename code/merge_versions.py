"""Merge per-version runs of step 3a or 3b into one output, as if all versions had been built in one run (F13).

On Skipjack each Slurm array task builds one version (--versions K). This script:
  - checks the parts are compatible: same options apart from --versions, and no version appears twice;
  - writes the data files with one header, in the same row order as a single run:
      3a (establishments): quarter by quarter, versions ascending within each quarter;
      3b (panel, exits, cells): version by version;
  - rebuilds the report over all versions from the parts' diagnostics (diag_<tag>.json.gz), so the medians,
    exactness maxima and flow profiles cover all versions.
Run from the project root (reports read QWI inputs by their relative paths).

Usage: python3 code/merge_versions.py {3a|3b} <out_dir> <part_dir> [<part_dir> ...]
"""
import argparse
import collections
import csv
import gzip
import os
import sys
from argparse import Namespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond  # noqa: E402

FILES = {'3a': ('establishments',), '3b': ('panel', 'exits', 'cells')}


def combine(diags, out):
    """Check compatibility; return (args namespace for the merged run, all versions, summed log)."""
    base = {k: v for k, v in diags[0]['args'].items() if k not in ('versions', 'out')}
    seen = []
    for d in diags:
        other = {k: v for k, v in d['args'].items() if k not in ('versions', 'out')}
        if other != base:
            diff = sorted(k for k in set(base) | set(other) if base.get(k) != other.get(k))
            sys.exit(f'parts differ in options other than --versions: {diff}')
        seen += d['versions']
    dup = [v for v, k in collections.Counter(seen).items() if k > 1]
    if dup: sys.exit(f'versions built more than once: {dup}')
    log = collections.Counter(); other = {}
    for d in diags:
        for k, v in d['log'].items():
            if isinstance(v, (int, float)): log[k] += v
            else: other.setdefault(k, v)
    for k, v in other.items(): log[k] = v   # non-counts (reclass rates) last, as in a single run
    a = Namespace(**dict(diags[0]['args'], out=out, versions=cond.versions_label(seen)))
    return a, sorted(seen), log


def readers(paths):
    out = []
    for p in paths:
        fh = cond.open_text(p); r = csv.reader(fh)
        out.append((fh, next(r), r))
    return out


def merge_by_quarter(paths, dst):
    """Interleave parts quarter by quarter (each part is ordered by quarter, then version, then cell)."""
    rs = readers(paths); header = rs[0][1]
    if any(h != header for _, h, _ in rs): sys.exit(f'headers differ in {dst}')
    iy, iq = header.index('year'), header.index('quarter')
    nxt = [next(r, None) for _, _, r in rs]
    with gzip.open(dst + '.part', 'wt', newline='') as fh:
        w = csv.writer(fh); w.writerow(header)
        while any(x is not None for x in nxt):
            key = min((int(x[iy]), int(x[iq])) for x in nxt if x is not None)
            for i, (_, _, r) in enumerate(rs):
                while nxt[i] is not None and (int(nxt[i][iy]), int(nxt[i][iq])) == key:
                    w.writerow(nxt[i]); nxt[i] = next(r, None)
    os.replace(dst + '.part', dst)


def concatenate(paths, dst):
    rs = readers(paths); header = rs[0][1]
    if any(h != header for _, h, _ in rs): sys.exit(f'headers differ in {dst}')
    with gzip.open(dst + '.part', 'wt', newline='') as fh:
        w = csv.writer(fh); w.writerow(header)
        for _, _, r in rs: w.writerows(r)
    os.replace(dst + '.part', dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('step', choices=('3a', '3b')); ap.add_argument('out'); ap.add_argument('parts', nargs='+')
    m = ap.parse_args()
    tag_of = lambda p: next(f[len('diag_'):-len('.json.gz')] for f in os.listdir(p) if f.startswith('diag_') and f.endswith('.json.gz'))
    if m.step == '3a':
        import create_establishments as mod
    else:
        import build_panel as mod
    diags = [mod.load_diag(os.path.join(p, f'diag_{tag_of(p)}.json.gz')) for p in m.parts]
    order = sorted(range(len(diags)), key=lambda i: min(diags[i]['versions']))
    parts = [m.parts[i] for i in order]; diags = [diags[i] for i in order]
    a, V, log = combine(diags, m.out)
    os.makedirs(m.out, exist_ok=True)
    for name in FILES[m.step]:
        src = [os.path.join(p, f'{name}_{a.tag}.csv.gz') for p in parts]
        dst = os.path.join(m.out, f'{name}_{a.tag}.csv.gz')
        (merge_by_quarter if m.step == '3a' else concatenate)(src, dst)
    run = cond.run_line() + ''.join(f'\n  part (versions {cond.versions_label(d["versions"])}): {d["run"]}' for d in diags)
    if m.step == '3a':
        checks = [x for d in diags for x in d['checks']]
        mod.save_diag(a, V, log, checks)
        mod.report(a, V, log, checks, run=run)
    else:
        exact = [max(d['exact'][i] for d in diags) for i in range(5)]
        gap = collections.defaultdict(list)
        for d in diags:
            for k, v in d['margin_gap'].items(): gap[k] += v
        mod.save_diag(a, V, log, exact, gap)
        mod.report(a, os.path.join(m.out, f'cells_{a.tag}.csv.gz'), exact, gap, log, run=run)


if __name__ == '__main__':
    main()
