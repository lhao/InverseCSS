"""Diagnostics for QWI stocks and flows before imputation (read-only).

For the county x 4-digit QWI files (sa, se, rh; all firms; private):
  A. status-flag shares per indicator, for the pilot slices and for all county 4-digit cells
  B. accounting identities between published indicators (all-worker totals)
  C. consistency of the three demographic breakdowns (sa, se, rh) with the total

Usage: python3 diagnose_qwi.py <qwi_dir> <out_dir>
Slices: Worcester (24047) x 7211, 7225; Montgomery (24031) x 5417.
"""
import collections, csv, gzip, os, statistics as st, sys

qwi_dir, out_dir = sys.argv[1:3]
SLICES = {('24047', '7211'), ('24047', '7225'), ('24031', '5417')}
STOCKS = ['Emp', 'EmpEnd', 'EmpS', 'EmpTotal', 'EmpSpv']
FLOWS = ['HirA', 'HirN', 'HirR', 'Sep', 'HirAEnd', 'SepBeg', 'HirAEndRepl', 'HirAS', 'HirNS', 'SepS', 'SepSnx',
         'FrmJbGn', 'FrmJbLs', 'FrmJbC', 'FrmJbGnS', 'FrmJbLsS', 'FrmJbCS']
RATES = ['HirAEndR', 'SepBegR', 'HirAEndReplR', 'TurnOvrS']
EARN = ['EarnS', 'EarnBeg', 'EarnHirAS', 'EarnHirNS', 'EarnSepS', 'Payroll']
ALL = STOCKS + FLOWS + RATES + EARN
FLAG = {'1': 'ok', '9': 'fuzzed', '5': 'suppressed', '-1': 'not computable', '-2': 'no data', '6': 'derived', '7': 'derived, distorted',
        '10': 'aggregate', '11': 'aggregate not released', '12': 'aggregate, distorted'}


def rows(tab):
    with gzip.open(os.path.join(qwi_dir, f'qwi_md_{tab}_op_u_2021-2024.csv.gz'), 'rt') as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] == 'C' and r['ind_level'] == '4':
                yield r


def total(r, tab):
    return {'sa': r['sex'] == '0' and r['agegrp'] == 'A00', 'se': r['sex'] == '0' and r['education'] == 'E0',
            'rh': r['race'] == 'A0' and r['ethnicity'] == 'A0'}[tab]


def val(r, v):
    return float(r[v]) if r[v] != '' and r['s' + v] in ('1', '9', '6', '7', '10', '12') else None


out = []
def say(s=''):
    print(s); out.append(s)

# ---------- load totals (sa) and demographic partitions ----------
tot, parts = {}, collections.defaultdict(lambda: collections.defaultdict(dict))
flags_slice, flags_all = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
for tab in ('sa', 'se', 'rh'):
    for r in rows(f'{tab}_f_gc_n4'):
        k = (r['geography'], r['industry'], int(r['year']), int(r['quarter']))
        if total(r, tab):
            if tab == 'sa':
                tot[k] = r
                for v in ALL:
                    flags_all[v][FLAG.get(r['s' + v], r['s' + v])] += 1
                    if (k[0], k[1]) in SLICES: flags_slice[v][FLAG.get(r['s' + v], r['s' + v])] += 1
            continue
        # one-way partitions that should add to the total
        if tab == 'sa' and r['sex'] == '0': p = 'age'
        elif tab == 'sa' and r['agegrp'] == 'A00': p = 'sex'
        elif tab == 'se' and r['sex'] == '0': p = 'education'
        elif tab == 'rh' and r['ethnicity'] == 'A0': p = 'race'
        elif tab == 'rh' and r['race'] == 'A0': p = 'ethnicity'
        else: continue   # two-way cells (sex x age etc.), not used here
        parts[p][k][r['sex'] + r['agegrp'] + r['education'] + r['race'] + r['ethnicity']] = r

# ---------- A. status flags ----------
say('A. Share of county x 4-digit all-worker cells by status flag, 2021-2024')
say(f'   slices: {sorted(SLICES)} ({sum(flags_slice["Emp"].values())} cell-quarters); all county 4-digit: {sum(flags_all["Emp"].values())} cell-quarters')
cats = ['ok', 'fuzzed', 'suppressed', 'not computable', 'no data']
say(f'   {"indicator":12s} {"slices: ok/fuzzed/suppr/not-comp/no-data":45s} | all county 4-digit')
for v in ALL:
    def fmt(c):
        n = sum(c.values()) or 1
        return '/'.join(f'{100 * c[x] / n:3.0f}' for x in cats)
    say(f'   {v:12s} {fmt(flags_slice[v]):45s} | {fmt(flags_all[v])}')

# ---------- B. identities ----------
say('\nB. Accounting identities on published all-worker cells (county x 4-digit, 2021-2024)')
prev = {(k[0], k[1], k[2] * 4 + k[3]): r for k, r in tot.items()}
tests = {
    'FrmJbC = FrmJbGn - FrmJbLs': lambda r, p: (val(r, 'FrmJbC'), sub(val(r, 'FrmJbGn'), val(r, 'FrmJbLs'))),
    'FrmJbC = EmpEnd - Emp': lambda r, p: (val(r, 'FrmJbC'), sub(val(r, 'EmpEnd'), val(r, 'Emp'))),
    'EmpEnd - Emp = HirA - Sep': lambda r, p: (sub(val(r, 'EmpEnd'), val(r, 'Emp')), sub(val(r, 'HirA'), val(r, 'Sep'))),
    'EmpEnd - Emp = HirAEnd - SepBeg': lambda r, p: (sub(val(r, 'EmpEnd'), val(r, 'Emp')), sub(val(r, 'HirAEnd'), val(r, 'SepBeg'))),
    'HirA = HirN + HirR': lambda r, p: (val(r, 'HirA'), add(val(r, 'HirN'), val(r, 'HirR'))),
    'EmpTotal = Emp + HirA': lambda r, p: (val(r, 'EmpTotal'), add(val(r, 'Emp'), val(r, 'HirA'))),
    'FrmJbCS = FrmJbGnS - FrmJbLsS': lambda r, p: (val(r, 'FrmJbCS'), sub(val(r, 'FrmJbGnS'), val(r, 'FrmJbLsS'))),
    'Emp(t) = EmpEnd(t-1)': lambda r, p: (val(r, 'Emp'), val(p, 'EmpEnd') if p else None),
}
def sub(a, b): return a - b if a is not None and b is not None else None
def add(a, b): return a + b if a is not None and b is not None else None
ineq = {'EmpS <= Emp': ('EmpS', 'Emp'), 'EmpS <= EmpEnd': ('EmpS', 'EmpEnd'), 'HirAS <= HirA': ('HirAS', 'HirA'),
        'SepS <= Sep': ('SepS', 'Sep'), 'FrmJbGn <= HirA': ('FrmJbGn', 'HirA'), 'FrmJbLs <= Sep': ('FrmJbLs', 'Sep'),
        'Emp <= EmpTotal': ('Emp', 'EmpTotal')}
say(f'   {"identity":34s} {"n":>7s} {"exact":>7s} {"|diff|<=2":>9s} {"median |diff|":>14s} {"median |diff|/size":>18s}')
for name, f in tests.items():
    d = []
    for k, r in tot.items():
        p = prev.get((k[0], k[1], k[2] * 4 + k[3] - 1))
        a, b = f(r, p)
        if a is None or b is None: continue
        size = max(val(r, 'Emp') or 0, 1)
        d.append((abs(a - b), abs(a - b) / size))
    if d:
        say(f'   {name:34s} {len(d):7d} {sum(x == 0 for x, _ in d) / len(d):7.1%} {sum(x <= 2 for x, _ in d) / len(d):9.1%} '
            f'{st.median(x for x, _ in d):14.1f} {st.median(y for _, y in d):18.3f}')
say('   inequalities:')
for name, (a, b) in ineq.items():
    pairs = [(val(r, a), val(r, b)) for r in tot.values()]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    say(f'   {name:34s} n={len(pairs):6d} holds {sum(x <= y for x, y in pairs) / len(pairs):6.1%}')

# ---------- C. demographic consistency ----------
say('\nC. Sum of one-way demographic cells vs the all-worker total (published cells only, county x 4-digit)')
say(f'   {"partition":10s} {"indicator":10s} {"n":>7s} {"exact":>7s} {"median |sum-total|/total":>26s} {"p90":>8s}')
for p, cells in parts.items():
    for v in ('Emp', 'HirA', 'Sep', 'FrmJbGn', 'FrmJbLs'):
        rel = []
        for k, cs in cells.items():
            t = tot.get(k)
            if not t or val(t, v) in (None, 0): continue
            xs = [val(c, v) for c in cs.values()]
            if any(x is None for x in xs): continue    # skip if any part suppressed
            rel.append(abs(sum(xs) - val(t, v)) / val(t, v))
        if rel:
            rel.sort()
            say(f'   {p:10s} {v:10s} {len(rel):7d} {sum(x == 0 for x in rel) / len(rel):7.1%} {st.median(rel):26.4f} {rel[int(.9 * len(rel))]:8.4f}')

# ---------- slice snapshot ----------
say('\nD. Slice snapshot: all-worker totals by quarter (published values; blank = not published)')
cols = ['Emp', 'EmpEnd', 'EmpS', 'HirA', 'Sep', 'FrmJbGn', 'FrmJbLs', 'EarnS']
say('   ' + ' '.join(f'{c:>8s}' for c in ['county', 'naics', 'yq'] + cols))
for k in sorted(k for k in tot if (k[0], k[1]) in SLICES):
    r = tot[k]
    say('   ' + ' '.join(f'{x:>8s}' for x in [k[0], k[1], f'{k[2]}q{k[3]}'] + [f'{val(r, c):.0f}' if val(r, c) is not None else '' for c in cols]))

os.makedirs(out_dir, exist_ok=True)
open(os.path.join(out_dir, 'qwi_diagnostics_county4d_2021-2024.txt'), 'w').write('\n'.join(out) + '\n')
