"""Plasmode benchmark, stage 1 (design section 8a): can the worker-group reconstruction recover a KNOWN structure?

Truth: the systematic worker groups (version 1, stocks and flows by worker type per establishment) with two planted
structures, each establishment's totals kept:
  P1  firm size: in firms of 0-19, workers under 25 (A01-A03) x 1.5 (small firms younger than the state pattern says)
  P2  a joint never published: Hispanic workers without a high-school diploma (re H, E1) x 1.5
Tabulation, stage 1 (complete tables: no noise, no suppression), QWI-style one breakdown at a time:
  county x 4-digit industry: sex x age, sex x education, race x ethnicity (stocks, hires, separations)
The state 6-digit x firm-size tables, ACS and LODES are the real ones (common priors), and the establishment panel
(sizes, firm classes) is the truth's: stage 1 tests the worker-composition step alone.
Reconstruction: worker_groups.py (random and systematic, version 1) on the fake county tables, with a DIFFERENT random
seed from the truth's (the same seed regenerates the truth's own establishment tilts: a circular test, found 2026-10-18).
Caveat: the truth is built from the systematic version, so it shares the systematic prior (ACS joint, LODES dispersion);
only the planted parts are unknown to it. A fairer later test builds the truth from an independent structure (8b).
Measures, truth vs reconstruction:
  young share of employment by firm size class (P1)     share of Hispanic workers with E1 (P2)
  establishment-level: correlation of young share and of Hispanic share with the truth; dissimilarity (segregation)
  constraint check: county 4-digit compositions reproduced
Outputs (<out_dir>): truth_workers.csv.gz, fake_qwi/, recon_<prior>/, plasmode_report.txt
Usage: python3 plasmode_stage1.py <workers_sf_systematic.csv.gz> <panel_dir> <qwi_dir> <acs_workers.parquet> <lodes_wac> <out_dir>
"""
import argparse, collections, csv, gzip, math, os, statistics as st, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond

YOUNG = {'A01', 'A02', 'A03'}
RE_CODES = {'H': ('A1', 'A2'), 'W': ('A1', 'A1'), 'B': ('A2', 'A1'), 'A': ('A4', 'A1'), 'O': ('A3', 'A1')}
HDR = ['periodicity', 'seasonadj', 'geo_level', 'geography', 'ind_level', 'industry', 'ownercode', 'sex', 'agegrp', 'race', 'ethnicity',
       'education', 'firmage', 'firmsize', 'year', 'quarter', 'Emp', 'sEmp', 'EmpEnd', 'sEmpEnd', 'HirA', 'sHirA', 'Sep', 'sSep']


def plant(rows):
    """Apply P1 and P2 within an establishment-quarter; keep its totals of emp, hires, separations."""
    out = []
    fac = []
    for r in rows:
        f = 1.0
        if r['firm_size_class'] == '1' and r['agegrp'] in YOUNG: f *= 1.5
        if r['re'] == 'H' and r['education'] == 'E1': f *= 1.5
        fac.append(f)
    for col in ('emp', 'hires', 'separations'):
        tot = sum(float(r[col] or 0) for r in rows); new = sum(float(r[col] or 0) * f for r, f in zip(rows, fac))
        for r, f in zip(rows, fac): r[col + '_t'] = float(r[col] or 0) * f * (tot / new) if new > 0 else 0.0
    return rows


def main():
    ap = argparse.ArgumentParser()
    for x in ('workers', 'panel', 'qwi', 'acs', 'lodes', 'out'): ap.add_argument(x)
    ap.add_argument('--noise', default='none', choices=('none', 'mild', 'harsh'),
                    help='stage 2: QWI-style noise infusion, one fixed factor per establishment with |factor - 1| uniform in [0.01, 0.05] (mild) or [0.05, 0.15] (harsh); ASSUMED ranges, LEHD parameters are not public')
    ap.add_argument('--suppress', type=float, default=0.0, help='stage 3: suppress group cells (flag 5) with fewer than this many jobs (QWI: 0-2 jobs generally suppressed; assumed 3)')
    a = ap.parse_args()
    cond.start_run(a.workers, a.panel, a.qwi)
    os.makedirs(a.out, exist_ok=True)
    # 1. truth (version 1)
    groups = collections.defaultdict(list)
    with cond.open_text(cond.require(a.workers)) as fh:
        for r in csv.DictReader(fh):
            if r['version'] == '1': groups[(r['year'], r['quarter'], r['est_id'])].append(r)
    truth = []
    for k, rows in groups.items(): truth.extend(plant(rows))
    with gzip.open(os.path.join(a.out, 'truth_workers.csv.gz'), 'wt', newline='') as fo:
        w = csv.writer(fo); w.writerow(['year', 'quarter', 'county', 'naics6', 'est_id', 'firm_size_class', 'sex', 'agegrp', 'education', 're', 'emp', 'hires', 'separations'])
        for r in truth: w.writerow([r['year'], r['quarter'], r['county'], r['naics6'], r['est_id'], r['firm_size_class'], r['sex'], r['agegrp'], r['education'], r['re'],
                                    round(r['emp_t'], 4), round(r['hires_t'], 4), round(r['separations_t'], 4)])
    # 2. tabulate county x 4-digit, one breakdown at a time
    tab = {n: collections.defaultdict(lambda: [0.0, 0.0, 0.0]) for n in ('sa', 'se', 'rh', 'tot')}
    import random as _r
    rng = _r.Random(20261019); fuzz = {}
    lo_hi = {'none': None, 'mild': (0.01, 0.05), 'harsh': (0.05, 0.15)}[a.noise]
    for r in truth:
        base = (r['county'], r['naics6'][:4], r['year'], r['quarter'])
        f = 1.0
        if lo_hi:
            if r['est_id'] not in fuzz:
                mag = lo_hi[0] + rng.random() * (lo_hi[1] - lo_hi[0]); fuzz[r['est_id']] = 1 + mag if rng.random() < 0.5 else 1 - mag
            f = fuzz[r['est_id']]
        v = (r['emp_t'] * f, r['hires_t'] * f, r['separations_t'] * f)
        for n, g in (('sa', (r['sex'], r['agegrp'])), ('se', (r['sex'], r['education'])), ('rh', (r['re'],)), ('tot', ())):
            t = tab[n][base + g]
            for i in range(3): t[i] += v[i]
    fq = os.path.join(a.out, 'fake_qwi'); os.makedirs(fq, exist_ok=True); supp_count = collections.Counter()
    for n in ('sa', 'se', 'rh'):
        with gzip.open(os.path.join(fq, f'qwi_md_{n}_f_gc_n4_op_u_2021-2024.csv.gz'), 'wt', newline='') as fo:
            w = csv.writer(fo); w.writerow(HDR)
            def row(c, ind, y, q, sex, age, race, eth, edu, v, total=False):
                vals = []
                for x in (v[0], v[0], v[1], v[2]):
                    if not total and a.suppress and x < a.suppress: vals += ['', 5]; supp_count[n] += 1
                    else: vals += [x, 1]
                w.writerow(['Q', 'S', 'C', c, 'N4', ind, 'A05', sex, age, race, eth, edu, '0', '0', y, q] + vals)
            for (c, ind, y, q), v in tab['tot'].items(): row(c, ind, y, q, '0', 'A00', 'A0', 'A0', 'E0', v, total=True)
            for k, v in tab[n].items():
                c, ind, y, q = k[:4]; g = k[4:]
                if n == 'sa': row(c, ind, y, q, g[0], g[1], 'A0', 'A0', 'E0', v)
                elif n == 'se': row(c, ind, y, q, g[0], 'A00', 'A0', 'A0', g[1], v)
                else: rc, ec = RE_CODES[g[0]]; row(c, ind, y, q, '0', 'A00', rc, ec, 'E0', v)
    for f in os.listdir(a.qwi):   # real state tables and the rest
        if f.endswith('.csv.gz') and '_f_gc_n4_' not in f:
            dst = os.path.join(fq, f)
            if not os.path.exists(dst): os.symlink(os.path.join(os.path.abspath(a.qwi), f), dst)
    # 3. reconstruct
    here = os.path.dirname(os.path.abspath(__file__))
    for prior in ('random', 'systematic'):
        od = os.path.join(a.out, f'recon_{prior}')
        if not os.path.exists(os.path.join(od, 'workers_slices.csv.gz')):
            subprocess.run([sys.executable, os.path.join(here, 'worker_groups.py'), a.panel, fq, a.acs, a.lodes, od, '--prior', prior, '--draws', '1',
                            '--seed', '9721'],   # a seed different from the truth's, else the systematic tilts are regenerated (circular)
                           check=True, stdout=subprocess.DEVNULL)
    # 4. compare
    def load(path, cols):
        D = collections.defaultdict(lambda: collections.defaultdict(float))
        with cond.open_text(path) as fh:
            for r in csv.DictReader(fh):
                if r.get('version', '1') != '1': continue
                D[(r['year'], r['quarter'], r['est_id'])][(r['firm_size_class'], r['county'], r['naics6'][:4], r['sex'], r['agegrp'], r['education'], r['re'])] += float(r[cols])
        return D
    T = load(os.path.join(a.out, 'truth_workers.csv.gz'), 'emp')
    R = {p: load(os.path.join(a.out, f'recon_{p}', 'workers_slices.csv.gz'), 'emp') for p in ('random', 'systematic')}
    def summary(D):
        young = collections.defaultdict(lambda: [0.0, 0.0]); hisp = collections.defaultdict(lambda: [0.0, 0.0]); est = {}
        for k, d in D.items():
            tot = sum(d.values()); y_ = sum(v for t, v in d.items() if t[4] in YOUNG); h_ = sum(v for t, v in d.items() if t[6] == 'H')
            he1 = sum(v for t, v in d.items() if t[6] == 'H' and t[5] == 'E1'); hold = sum(v for t, v in d.items() if t[6] == 'H' and t[5] != 'E5')
            any_t = next(iter(d)); fs, c, i4 = any_t[0], any_t[1], any_t[2]
            young[(c, i4, fs)][0] += y_; young[(c, i4, fs)][1] += tot
            hisp[(c, i4)][0] += he1; hisp[(c, i4)][1] += hold
            if tot > 0: est[k] = (tot, y_ / tot, h_ / tot)
        return young, hisp, est
    S = {'truth': summary(T)} | {p: summary(D) for p, D in R.items()}
    L = [f'Plasmode: worker-composition reconstruction vs a known truth (slices, version 1; county 4-digit tables, noise: {a.noise}, suppression below {a.suppress:g} jobs: {dict(supp_count)} values)',
         'Planted: P1 young (<25) x 1.5 in firms of 0-19; P2 Hispanic without high-school diploma x 1.5', '',
         'P1: young share of employment by firm size class (0-19 ... 500+)']
    for c, i4 in sorted({(k[0], k[1]) for k in S['truth'][0]}):
        for lab in ('truth', 'random', 'systematic'):
            yg = S[lab][0]
            L.append(f'  {c} {i4} {lab:10s} ' + '  '.join(f'{fs}: {yg[(c, i4, fs)][0] / yg[(c, i4, fs)][1]:.3f}' for fs in '12345' if yg.get((c, i4, fs), [0, 0])[1] > 0))
    L.append('\nP2: share of Hispanic workers (25+) without a high-school diploma')
    for c, i4 in sorted(S['truth'][1]):
        L.append(f'  {c} {i4}: ' + '  '.join(f'{lab} {S[lab][1][(c, i4)][0] / S[lab][1][(c, i4)][1]:.3f}' for lab in ('truth', 'random', 'systematic') if S[lab][1].get((c, i4), [0, 0])[1] > 0))
    L.append('\nEstablishment level (employment-weighted correlation with the truth, over establishment-quarters):')
    def wcorr(xs):
        sw = sum(w for w, _, _ in xs); mx = sum(w * x for w, x, _ in xs) / sw; my = sum(w * y for w, _, y in xs) / sw
        cv = sum(w * (x - mx) * (y - my) for w, x, y in xs); vx = sum(w * (x - mx) ** 2 for w, x, _ in xs); vy = sum(w * (y - my) ** 2 for w, _, y in xs)
        return cv / math.sqrt(vx * vy) if vx > 0 and vy > 0 else float('nan')
    for p in ('random', 'systematic'):
        common = [k for k in S['truth'][2] if k in S[p][2]]
        cy = wcorr([(S['truth'][2][k][0], S['truth'][2][k][1], S[p][2][k][1]) for k in common])
        ch = wcorr([(S['truth'][2][k][0], S['truth'][2][k][2], S[p][2][k][2]) for k in common])
        L.append(f'  {p:10s}: young share r = {cy:.3f}; Hispanic share r = {ch:.3f} (n = {len(common):,})')
    # noise effect: county x 4-digit sex x age composition, reconstruction vs truth (TVD, mean over cell-quarters)
    def comp(D):
        C = collections.defaultdict(collections.Counter)
        for k, d in D.items():
            for t, v in d.items(): C[(k[0], k[1], t[1], t[2])][(t[3], t[4])] += v
        return C
    CT = comp(T)
    for p in ('random', 'systematic'):
        CR = comp(R[p]); g = []
        for k, ct in CT.items():
            cr = CR.get(k)
            if not cr: continue
            tt, tr = sum(ct.values()), sum(cr.values())
            if tt > 50 and tr > 0: g.append(sum(abs(ct[x] / tt - cr.get(x, 0) / tr) for x in set(ct) | set(cr)) / 2)
        L.append(f'  county 4-digit sex x age composition vs truth ({p}): TVD mean {st.mean(g):.4f} (cell-quarters >= 50 jobs: {len(g)}); noise: {a.noise}')
    L.append('\nNote: establishment-level recovery is not expected beyond what firm size and industry predict; the truth\'s')
    L.append('establishment heterogeneity comes from its own random tilts, which no public table reveals.')
    print('\n'.join(L)); open(os.path.join(a.out, 'plasmode_report.txt'), 'w').write('\n'.join(L) + '\n')


if __name__ == '__main__':
    main()
