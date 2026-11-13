"""Checks on downscaled county x 6-digit QWI output.

A. County sums (mean of draws) vs QWI state x 6-digit totals, with the state target scaled by the share of the
   industry's QCEW employment located in counties that could be downscaled (coverage).
B. Pilot-slice table for one year.

Usage: python3 check_downscale.py <downscaled.csv.gz> <qwi_sa_f_gs_n6.csv.gz> <qcew_raw_root> <qcew_imputed_dir> <year> <out.txt>
                                  [<qcew_complete_dir>]
"""
import collections, csv, gzip, math, os, re, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond

out_path, qwi, raw, imp, year, report = sys.argv[1:7]
complete_dir = sys.argv[7] if len(sys.argv) > 7 else ''
year = int(year)
V = ('Emp', 'EmpS', 'HirA', 'HirN', 'Sep', 'FrmJbGn', 'FrmJbLs', 'EarnS')
acc = collections.defaultdict(lambda: collections.defaultdict(list))
with gzip.open(out_path, 'rt') as fh:
    for r in csv.DictReader(fh):
        k = (r['area_fips'], r['industry_code'], int(r['year']), int(r['quarter']))
        for v in V:
            if r[v] != '': acc[k][v].append(float(r[v]))
mean = {k: {v: st.mean(x) for v, x in d.items()} for k, d in acc.items()}

state = {}
with gzip.open(qwi, 'rt') as fh:
    for r in csv.DictReader(fh):
        if r['geo_level'] == 'S' and r['ind_level'] == '6' and r['sex'] == '0' and r['agegrp'] == 'A00' and r['ownercode'] == 'A05':
            state[(r['industry'], int(r['year']), int(r['quarter']))] = r

cov_num, cov_den = collections.defaultdict(float), collections.defaultdict(float)
for y, q, qdir in cond.quarter_dirs(raw):
    d = os.path.basename(qdir); vals = {}
    cp = os.path.join(complete_dir, f'qcew_complete_{y}q{q}_M20.csv.gz') if complete_dir else ''
    if cp and os.path.exists(cp):          # NAICS-2022 converted quarter (C1)
        with cond.open_text(cp) as fh:
            for r in csv.DictReader(fh):
                if r['draw'] == '1' and r['own_code'] == '5' and r['level'] == '6' and r['area_fips'] != '24000':
                    vals[(r['area_fips'], r['industry_code'])] = float(r['month1_emplvl'])
        for (c, ind), m1 in vals.items():
            cov_den[(ind, y, q)] += m1
            if (c, ind, y, q) in mean: cov_num[(ind, y, q)] += m1
        continue
    for f in os.listdir(os.path.join(raw, d)):
        if not re.fullmatch(r'24\d{3}\.csv', f) or f == '24000.csv': continue
        for r in csv.DictReader(open(os.path.join(raw, d, f))):
            if r['agglvl_code'] == '78' and r['own_code'] == '5' and r['disclosure_code'] in ('', '-'):
                vals[(r['area_fips'], r['industry_code'])] = float(r['month1_emplvl'])
    p = os.path.join(imp, f'qcew_imputed_{y}q{q}_M20.csv.gz')
    if not os.path.exists(p): raise SystemExit(f'missing {p}')
    with gzip.open(p, 'rt') as fh:
        for r in csv.DictReader(fh):
            if r['draw'] == '1' and r['own_code'] == '5' and r['level'] == '6':
                vals[(r['area_fips'], r['industry_code'])] = float(r['month1_emplvl'])
    for (c, ind), m1 in vals.items():
        cov_den[(ind, y, q)] += m1
        if (c, ind, y, q) in mean: cov_num[(ind, y, q)] += m1

sums = collections.defaultdict(lambda: collections.defaultdict(float))
for (c, ind, y, q), d in mean.items():
    for v in ('Emp', 'HirA', 'FrmJbGn'): sums[(ind, y, q)][v] += d.get(v, 0.0)

lines = ['A. County sums of downscaled 6-digit cells vs QWI state 6-digit totals (private; mean of draws)',
         '   state target x coverage, coverage = share of the industry-quarter QCEW employment in downscaled counties']
for v in ('Emp', 'HirA', 'FrmJbGn'):
    lr, full = [], []
    for k, s in sums.items():
        t = state.get(k)
        if not t or t['s' + v] not in ('1', '9') or t[v] == '' or cov_den[k] <= 0 or s[v] <= 0: continue
        cv = cov_num[k] / cov_den[k]; tv = float(t[v]) * cv
        if tv <= 0: continue
        x = abs(math.log(s[v] / tv)); lr.append(x)
        if cv > 0.95: full.append(x)
    lr.sort(); full.sort()
    lines.append(f'   {v:8s} n={len(lr):5d} median |log ratio|={st.median(lr):.3f} p90={lr[int(.9 * len(lr))]:.3f} | '
                 f'coverage>95%: n={len(full)} median={st.median(full) if full else float("nan"):.3f}')
cov = [cov_num[k] / cov_den[k] for k in cov_den if cov_den[k] > 0]
lines.append(f'   coverage: median {st.median(cov):.2f}, mean {st.mean(cov):.2f} (industry-quarters with QCEW employment)')

lines += ['', f'B. Pilot slices, {year}: mean of draws',
          '   ' + f'{"county":6s} {"naics":7s} {"q":3s} ' + ' '.join(f'{v:>8s}' for v in V)]
for k in sorted(mean):
    if k[2] == year and ((k[0] == '24047' and k[1][:4] in ('7211', '7225')) or (k[0] == '24031' and k[1][:4] == '5417')):
        d = mean[k]
        lines.append('   ' + f'{k[0]:6s} {k[1]:7s} Q{k[3]} ' + ' '.join(f'{d.get(v, float("nan")):8.0f}' for v in V))
print('\n'.join(lines))
open(report, 'w').write('\n'.join(lines) + '\n')
