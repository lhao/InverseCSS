"""Summary of the QCEW imputation ablation (drafts/ablation_note.md): arms A impute only, B rake only, C joint, D hot-deck.

Reads <root>/<arm>/validation_<y>q<q>_alpha2.csv (hidden published cells with 1-3 establishments; truth and each method's
mean, p10, p90 over the draws) and the run logs (state-total gaps logged by rake_level). Arm D is the hot-deck columns
of the C runs. Checks first that every arm was scored on the same hidden cells.

Measures (month-3 employment and quarterly wages): median and mean |error| of the draw mean, mean bias, 80% interval
coverage and median width (no interval for B: one version), state 6-digit targets missed by > 2% and the total gap;
by establishment count (1, 2, 3): median |error| / truth, share within +-1 job, median width / truth; paired bootstrap
over cells (2,000 resamples) of the difference in mean |error| against C; median |error| by quarter.
Usage: python3 ablation_summary.py <root>
"""
import ast, csv, glob, os, random, re, statistics as st, sys

ARMS = (('A', 'A_impute_only', 'alloc', 'impute only'), ('B', 'B_rake_only', 'alloc', 'rake only'),
        ('C', 'C_joint', 'alloc', 'joint (pilot method)'), ('D', 'C_joint', 'hotdeck', 'hot-deck'))
VARS = (('month3_emplvl', 'month-3 employment (jobs)', '{:,.1f}'), ('total_qtrly_wages', 'quarterly wages ($)', '{:,.0f}'))
QS = [(y, q) for y in (2021, 2022, 2023, 2024) for q in (1, 2, 3, 4)]


def load(root, folder):
    rows = {}
    for y, q in QS:
        with open(f'{root}/{folder}/validation_{y}q{q}_alpha2.csv') as fh:
            for r in csv.DictReader(fh):
                rows[(y, q, r['area_fips'], r['own_code'], r['industry_code'])] = r
    return rows


def state_gaps(root, folder):
    """Level-6 state-total gaps summed over quarters (and draws), from the 'log:' line of each run."""
    tot = {}
    for f in glob.glob(f'{root}/{folder}/logs/impute_qcew_validation_*.log'):
        for line in open(f):
            if line.startswith('log: '):
                for k, v in ast.literal_eval(line[5:]).items():
                    if k.startswith('L6_'): tot[k] = tot.get(k, 0) + v
    return tot


def main():
    root = sys.argv[1]
    data = {a: load(root, folder) for a, folder, _, _ in ARMS}
    keys = sorted(data['C'])
    for a in data: assert sorted(data[a]) == keys, f'arm {a} was scored on different hidden cells'
    out = [f'QCEW imputation ablation, Maryland 2021-2024 (16 quarters); {len(keys)} hidden published cells '
           f'(1-3 establishments, with suppressed siblings), the same cells in every arm.', '']

    def series(a, v):
        meth = dict((x[0], x[2]) for x in ARMS)[a]
        res = []
        for k in keys:
            r = data[a][k]
            if not r.get(f'{meth}_{v}_mean'): res.append(None); continue
            m, lo, hi = (float(r[f'{meth}_{v}_{s}']) for s in ('mean', 'p10', 'p90'))
            tr = float(r[f'true_{v}'])
            res.append((m, lo, hi, tr, int(r['estabs']), k[0], k[1]))
        return res

    for v, lab, fmt in VARS:
        out.append(f'== {lab}')
        out.append(f'{"arm":24s} {"n":>5s} {"median|err|":>14s} {"mean|err|":>14s} {"mean bias":>14s} {"80% cover":>10s} {"median width":>14s}')
        S = {a: series(a, v) for a, *_ in ARMS}
        for a, _, _, name in ARMS:
            ok = [s for s in S[a] if s]
            ae = [abs(m - tr) for m, lo, hi, tr, *_ in ok]
            single = a == 'B'
            cov = '-' if single else f'{st.mean(1.0 if lo <= tr <= hi else 0.0 for m, lo, hi, tr, *_ in ok):.2f}'
            wid = '-' if single else fmt.format(st.median(hi - lo for m, lo, hi, *_ in ok))
            out.append(f'{a + " " + name:24s} {len(ok):5d} {fmt.format(st.median(ae)):>14s} {fmt.format(st.mean(ae)):>14s} '
                       f'{fmt.format(st.mean(m - tr for m, lo, hi, tr, *_ in ok)):>14s} {cov:>10s} {wid:>14s}')
        # paired bootstrap of mean |error| differences against C, on cells scored by both
        rng = random.Random(20261105)
        out.append('  paired difference in mean |error| vs C (arm - C), 95% bootstrap interval over cells (2,000 resamples):')
        for a in ('A', 'B', 'D'):
            pairs = [(abs(x[0] - x[3]), abs(c[0] - c[3])) for x, c in zip(S[a], S['C']) if x and c]
            d = [p - q for p, q in pairs]
            boots = sorted(st.mean(rng.choices(d, k=len(d))) for _ in range(2000))
            out.append(f'    {a}: {fmt.format(st.mean(d))}  [{fmt.format(boots[49])}, {fmt.format(boots[1949])}]  (n={len(d)})')
        # close but not the fact: by establishment count
        if v == 'month3_emplvl':
            out.append('  by establishment count (C joint; D hot-deck): n, median |err|/truth, share of draw means within +-1 job, median width/truth')
            for a in ('C', 'D'):
                for e in (1, 2, 3):
                    ok = [s for s in S[a] if s and s[4] == e]
                    pos = [s for s in ok if s[3] > 0]
                    if not ok: continue
                    out.append(f'    {a} estabs={e}: n={len(ok):4d}  median |err|/truth {st.median(abs(m - tr) / tr for m, lo, hi, tr, *_ in pos):.2f}  '
                               f'within +-1 job {st.mean(1.0 if abs(m - tr) <= 1 else 0.0 for m, lo, hi, tr, *_ in ok):.2f}  '
                               f'median width/truth {st.median((hi - lo) / tr for m, lo, hi, tr, *_ in pos):.2f}')
            out.append('  median |err| by quarter (jobs): ' + '  '.join(f'{a}' for a in ('A', 'B', 'C', 'D')))
            for y, q in QS:
                cells = [i for i, k in enumerate(keys) if k[:2] == (y, q)]
                vals = []
                for a in ('A', 'B', 'C', 'D'):
                    ae = [abs(S[a][i][0] - S[a][i][3]) for i in cells if S[a][i]]
                    vals.append(f'{st.median(ae):6.1f}' if ae else '     -')
                out.append(f'    {y}q{q} (n={len(cells):3d}) ' + ' '.join(vals))
        out.append('')

    out.append('== consistency with the published state 6-digit totals (level 6, summed over quarters and draws; A is measured, not raked)')
    for a, folder, meth, name in ARMS:
        if a == 'D': out.append('D hot-deck: donor values are not constrained by county or state totals'); continue
        g = state_gaps(root, folder)
        for v, lab, fmt in VARS:
            n, miss = g.get(f'L6_{v}_state_targets', 0), g.get(f'L6_{v}_state_misses_gt2pct', 0)
            gap, tgt = g.get(f'L6_{v}_abs_gap', 0.0), g.get(f'L6_{v}_target_total', 0.0)
            if n: out.append(f'{a} {name:22s} {lab:26s} targets {n:7,d}  missed by >2%: {miss / n:6.1%}  total gap / total target {gap / tgt if tgt else 0:6.2%}')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
