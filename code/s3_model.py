"""S3 (ANALYSIS_STRATEGY): AI exposure and job flows by firm-size stratum, before vs after generative AI (Maryland pilot).

Unit: county x 6-digit cell x firm-size stratum x quarter, 2021 Q2 - 2024 Q4, from the synthetic establishment panel
(job gains, job losses incl. exits; base = (start + end) / 2 employment). Rates in % per quarter. Cells with base >= 10.
Strata (S1, firm size primary): small < 50 (QWI classes 1-2), medium 50-499 (3-4), large 500+ (5).
  --strata age: firm age young 0-5 years (QWI classes 1-3), middle 6-10 (4), mature 11+ (5); --strata sizeage: size x age.
Exposure: the industry's occupation-weighted AI exposure (Eloundou et al. beta, GPT-4 rated; alternatives aioe,
lm_aioe), from ACS workplace 1-year 2021-2024 for the cell's ACS industry x county, shrunk towards the industry
statewide (k = 10 records); standardised to employment-weighted mean 0, SD 1 across cells. Fixed over time (C7).
Model per stratum and version (weighted by base employment):
    rate = beta * exposure x post + cell x quarter-of-year fixed effects + county x quarter fixed effects + error,
  post = 2023 Q1 onward. Cell x quarter-of-year effects remove each industry's own seasonal pattern (low-exposure
  hospitality grows every spring; with cell effects only, the event study showed a Q2 dip every year: B11, S2);
  --fe cell gives the cell-effects version as a sensitivity.
  two-way fixed effects removed by alternating weighted demeaning (numpy); SE clustered by ACS industry (the level at
  which exposure varies), CR1; wild cluster bootstrap p-value (Rademacher, 999 draws, unrestricted) for version 1.
Combining versions: Rubin's rules (pooled estimate, within + (1 + 1/M) x between variance).
Also: event study (exposure x quarter, reference 2022 Q4); placebo (data to 2022 Q4, fake post from 2022 Q2).
The pilot demonstrates the procedure; with one state, 15 quarters and M = 2 it cannot support substantive claims.

Outputs (<out_dir>): s3_estimates.csv, s3_event_study.csv, s3_report.txt
Usage: python3 s3_model.py <acs_workers.parquet> <out_dir> --panel random=<dir> --panel systematic=<dir> [--tag md] [--measure llm_beta_gpt4]
"""
import argparse, collections, csv, glob, hashlib, math, os, statistics as st, sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

STRATA = {'1': 'small <50', '2': 'small <50', '3': 'medium 50-499', '4': 'medium 50-499', '5': 'large 500+'}
AGES = {'1': 'young 0-5', '2': 'young 0-5', '3': 'young 0-5', '4': 'middle 6-10', '5': 'mature 11+'}
SIZE_ORDER, AGE_ORDER = ('small <50', 'medium 50-499', 'large 500+'), ('young 0-5', 'middle 6-10', 'mature 11+')
STRATA_BY = {'size': (lambda r: STRATA[r['firm_size_class']], SIZE_ORDER),
             'age': (lambda r: AGES[r['firm_age_class']], AGE_ORDER),
             'sizeage': (lambda r: STRATA[r['firm_size_class']] + ' x ' + AGES[r['firm_age_class']], tuple(f'{s} x {g}' for s in SIZE_ORDER for g in AGE_ORDER))}


def exposure_table(acs_path, measure, k=10.0):
    """{(county, naics6) -> exposure} function (closure), and the ACS industry key function."""
    import pyarrow.parquet as pq
    t = [p for p in pq.read_table(cond.require(acs_path), columns=['sample_type', 'sector', 'naics3', 'naics4', 'powpuma_counties', 'PERWT', measure]).to_pylist()
         if p['sample_type'] == '1-year' and p[measure] is not None]
    acc = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for p in t:
        areas = (p['powpuma_counties'] or '').replace(',', ' ').split()
        for kk in (('4', p['naics4']), ('3', p['naics3']), ('s', p['sector'])):
            if not kk[1]: continue
            for key in [(kk, 'MD')] + [(kk, ar) for ar in areas]:
                a_ = acc[key]; a_[0] += 1; a_[1] += p['PERWT']; a_[2] += p['PERWT'] * p[measure]
    have4 = {p['naics4'] for p in t if p['naics4']}; have3 = {p['naics3'] for p in t if p['naics3']}
    def ind_key(n6): return ('4', n6[:4]) if n6[:4] in have4 else (('3', n6[:3]) if n6[:3] in have3 else ('s', priors.bds_sector(n6)))
    def expo(county, n6):
        ik = ind_key(n6); top = acc.get((ik, 'MD'))
        if not top or top[1] <= 0: return None
        v = top[2] / top[1]; c = acc.get((ik, county))
        if c and c[1] > 0: w = c[0] / (c[0] + k); v = w * c[2] / c[1] + (1 - w) * v
        return v
    return expo, ind_key


def load_cells(pdir, tag, stratum_of=STRATA_BY['size'][0]):
    """{(version, county, naics6, stratum, y, q): [base, gains, losses]} from the panel and its exits."""
    D = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    with cond.open_text(cond.require(os.path.join(pdir, f'panel_{tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            if r['event'] == 'initial' or r['county'].endswith('999') or r['naics6'].startswith('99'): continue   # unknown county / unclassified
            k = (r['version'], r['county'], r['naics6'], stratum_of(r), int(r['year']), int(r['quarter']))
            d = D[k]; d[0] += (float(r['emp_prev_m3'] or 0) + float(r['emp_m3'])) / 2; d[1] += float(r['job_gain'] or 0); d[2] += float(r['job_loss'] or 0)
    with cond.open_text(cond.require(os.path.join(pdir, f'exits_{tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            if r['event'] != 'exit' or r['county'].endswith('999') or r['naics6'].startswith('99'): continue
            k = (r['version'], r['county'], r['naics6'], stratum_of(r), int(r['year']), int(r['quarter']))
            d = D[k]; x = float(r['emp_prev_m3']); d[0] += x / 2; d[2] += x
    return D


def load_est_exposure(root, lab, measure, tag='md'):
    """{(version, county, naics6, stratum): exposure}: employment-weighted mean of the establishments' own occupation-based
    exposure (occupation_exposure.py) in the cell and firm-size stratum, over 2021-2022 (fixed before the post period,
    so it cannot respond to the treatment). One value per version, so reconstruction uncertainty enters Rubin's rules."""
    acc = collections.defaultdict(lambda: [0.0, 0.0])
    files = sorted(glob.glob(os.path.join(root, lab, 'v*', 'g*', 'exposure', f'est_exposure_{tag}.csv.gz')))
    if not files: sys.exit(f'no establishment exposure files under {root}/{lab}')
    for f in files:
        with cond.open_text(f) as fh:
            for r in csv.DictReader(fh):
                if int(r['year']) > 2022 or r[measure] == '' or not r['firm_size_class']: continue
                e = float(r['emp'])
                if e <= 0: continue
                a_ = acc[(r['version'], r['county'], r['naics6'], STRATA[r['firm_size_class']])]; a_[0] += e; a_[1] += e * float(r[measure])
    return {k: v[1] / v[0] for k, v in acc.items() if v[0] > 0}, len(files)


def codes(keys):
    """Integer codes for a list of hashable keys."""
    m = {}; return np.array([m.setdefault(k, len(m)) for k in keys]), len(m)


def demean(Y, w, fes, iters=500, tol=1e-9):
    """Alternating weighted demeaning of the columns of Y (n x p) by each fixed-effect code vector in fes (two-way FE)."""
    Y = Y.copy()
    for _ in range(iters):
        delta = 0.0
        for c, G in fes:
            sw = np.bincount(c, weights=w, minlength=G)
            for j in range(Y.shape[1]):
                mj = np.bincount(c, weights=w * Y[:, j], minlength=G) / np.where(sw > 0, sw, 1)
                Y[:, j] -= mj[c]; delta = max(delta, float(np.abs(mj).max()))
        if delta < tol: break
    return Y


def fit(y, X, w, cl, G, rng, boot=999):
    """WLS of demeaned y on demeaned X (n x p); cluster-robust CR1 covariance; wild cluster bootstrap p-value for the
    first coefficient (unrestricted, Rademacher by cluster)."""
    n, p = X.shape
    XtWX = X.T @ (X * w[:, None])
    if np.linalg.matrix_rank(XtWX) < p or np.trace(XtWX) < 1e-8 * max(float(w.sum()), 1.0): return None   # not identified (e.g. placebo with one pre-year)
    inv = np.linalg.inv(XtWX); b = inv @ (X.T @ (w * y)); e = y - X @ b
    def vcov(res):
        S = np.zeros((G, p))
        for j in range(p): S[:, j] = np.bincount(cl, weights=w * X[:, j] * res, minlength=G)
        return inv @ (S.T @ S) @ inv * (G / (G - 1)) * ((n - 1) / (n - p - 1))
    V = vcov(e); se = np.sqrt(np.diag(V))
    pval = float('nan')
    if boot and G > 1:
        t0 = b[0] / se[0]; cnt = 0
        for _ in range(boot):
            sg = rng.choice([-1.0, 1.0], size=G)[cl]
            ys = X @ b + sg * e; bb = inv @ (X.T @ (w * ys)); es = ys - X @ bb
            vs = vcov(es)[0, 0]
            if vs > 0 and abs((bb[0] - b[0]) / math.sqrt(vs)) >= abs(t0): cnt += 1
        pval = cnt / boot
    return {'b': b, 'se': se, 'p_boot': pval, 'n': n, 'clusters': G}


def rubin(ests):
    M = len(ests); qb = st.mean(float(e['b'][0]) for e in ests); U = st.mean(float(e['se'][0]) ** 2 for e in ests)
    B = st.variance([float(e['b'][0]) for e in ests]) if M > 1 else 0.0
    T = U + (1 + 1 / M) * B
    return qb, math.sqrt(T), (B * (1 + 1 / M) / T if T > 0 else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('acs'); ap.add_argument('out')
    ap.add_argument('--panel', action='append', required=True); ap.add_argument('--tag', default='md')
    ap.add_argument('--fe', default='cellqoy', choices=('cellqoy', 'cell'), help='cellqoy: cell x quarter-of-year fixed effects (removes industry seasonality); cell: cell fixed effects')
    ap.add_argument('--time-fe', default='county', choices=('county', 'countysector'), help='county x quarter, or county x sector x quarter (within-sector comparison)')
    ap.add_argument('--from-year', type=int, default=2021, help='first year of the sample (2022 drops the 2021 pandemic recovery)')
    ap.add_argument('--workers', default='', help='worker-stage root (e.g. runs/workers_m20): exposure from the establishments\' own occupation mix by cell x firm-size stratum and version (2021-22 mean); default: ACS industry x county')
    ap.add_argument('--strata', default='size', choices=tuple(STRATA_BY), help='firm size (default), firm age, or size x age')
    ap.add_argument('--measure', default='llm_beta_gpt4'); ap.add_argument('--min-base', type=float, default=10.0); ap.add_argument('--seed', type=int, default=31)
    a = ap.parse_args()
    if a.workers and a.strata != 'size': sys.exit('--workers exposure is by firm-size stratum; use it with --strata size')
    stratum_of, order = STRATA_BY[a.strata]
    cond.start_run(a.acs, *[p.split('=', 1)[1] for p in a.panel])
    expo, ind_key = exposure_table(a.acs, a.measure)
    os.makedirs(a.out, exist_ok=True)
    est_rows, es_rows = [], []
    L = [f'S3 pilot: AI exposure ({a.measure}, standardised' + ('; from establishments' if a.workers else '; ACS industry x county') + ') x post (2023 Q1+) on job flows by firm-' + {'size': 'size', 'age': 'age', 'sizeage': 'size x age'}[a.strata] + f' stratum; fixed effects: {a.fe} + {a.time_fe} x quarter; sample from {a.from_year}; cells with base >= {a.min_base:g}',
         'Coefficients: percentage points per quarter per 1 SD of exposure. Rubin\'s rules over versions (fraction of variance due to versions in brackets).', '']
    for lab, pdir in (p.split('=', 1) for p in a.panel):
        D = load_cells(pdir, a.tag, stratum_of)
        versions = sorted({k[0] for k in D})
        if a.workers:
            EW, nfiles = load_est_exposure(a.workers, lab, a.measure, a.tag); src_count = collections.Counter()
            def vexp(k):
                v = EW.get((k[0], k[1], k[2], k[3]))
                if v is None: src_count['acs_fallback'] += 1; return expo(k[1], k[2])
                src_count['establishments'] += 1; return v
        else:
            def vexp(k): return expo(k[1], k[2])
        # exposure standardisation (employment-weighted over cells, version 1)
        ev = [(d[0], vexp(k)) for k, d in D.items() if k[0] == versions[0] and d[0] >= a.min_base]
        ev = [(w_, v) for w_, v in ev if v is not None]
        mu = sum(w_ * v for w_, v in ev) / sum(w_ for w_, _ in ev); sd = math.sqrt(sum(w_ * (v - mu) ** 2 for w_, v in ev) / sum(w_ for w_, _ in ev))
        L.append(f'== panel {lab}: exposure mean {mu:.3f}, SD {sd:.3f} (employment-weighted over cell-quarters)')
        if a.workers: L.append(f'   exposure source: establishments\' own occupation mix ({nfiles} worker files, cell x stratum x version, 2021-22 mean); lookups {dict(src_count)}')
        for stratum in order:
            for outcome in ('gain', 'loss', 'net'):
                for spec in ('main', 'placebo'):
                    per_v = []
                    for m in versions:
                        rows, w, ycol, xcol = [], [], [], []
                        for k, d in D.items():
                            if k[0] != m or k[3] != stratum or d[0] < a.min_base or k[4] < a.from_year: continue
                            yq = (k[4], k[5])
                            if spec == 'placebo' and yq > (2022, 4): continue
                            v = vexp(k)
                            if v is None: continue
                            post = (yq >= (2023, 1)) if spec == 'main' else (yq >= (2022, 2))
                            rate = {'gain': 100 * d[1] / d[0], 'loss': 100 * d[2] / d[0], 'net': 100 * (d[1] - d[2]) / d[0]}[outcome]
                            rows.append(k); w.append(d[0]); ycol.append(rate); xcol.append(((v - mu) / sd) * (1.0 if post else 0.0))
                        if len(rows) < 30: continue
                        wv = np.array(w); c1, g1 = codes([(r[1], r[2], r[5]) if a.fe == 'cellqoy' else (r[1], r[2]) for r in rows]); c2, g2 = codes([(r[1], r[4], r[5]) + ((priors.bds_sector(r[2]),) if a.time_fe == 'countysector' else ()) for r in rows])
                        Z = demean(np.column_stack([ycol, xcol]), wv, [(c1, g1), (c2, g2)])
                        cl, G = codes([ind_key(r[2]) for r in rows])
                        res = fit(Z[:, 0], Z[:, 1:2], wv, cl, G, np.random.default_rng(int(hashlib.md5(f'{a.seed}|{lab}|{stratum}|{outcome}|{spec}|{m}'.encode()).hexdigest()[:8], 16)), boot=999 if m == versions[0] else 0)   # fixed seed (F4)
                        if res: res['version'] = m; per_v.append(res)
                    if not per_v: continue
                    qb, se, fmi = rubin(per_v)
                    est_rows.append({'panel': lab, 'stratum': stratum, 'outcome': outcome, 'spec': spec, 'beta': qb, 'se': se, 'versions_share_of_variance': fmi,
                                     'p_boot_v1': per_v[0]['p_boot'], 'n_v1': per_v[0]['n'], 'clusters': per_v[0]['clusters'], 'versions': len(per_v)})
            L.append(f'  {stratum:{max(14, max(map(len, order)))}s} ' + '   '.join(f'{o}: ' + '  '.join(
                f'{s_} {r["beta"]:+.3f} ({r["se"]:.3f}) [{r["versions_share_of_variance"]:.2f}] p={r["p_boot_v1"]:.2f}'
                for s_ in ('main', 'placebo') for r in est_rows if r['panel'] == lab and r['stratum'] == stratum and r['outcome'] == o and r['spec'] == s_)
                for o in ('gain', 'loss', 'net')))
        # event study (net change, all strata pooled per stratum), version 1
        for stratum in order:
            m = versions[0]; rows, w, ycol, X = [], [], [], []
            qs = sorted({(k[4], k[5]) for k in D if k[4] >= a.from_year})
            refs = {(2022, 1), (2022, 2), (2022, 3), (2022, 4)} if a.fe == 'cellqoy' else {(2022, 4)}   # with cell x quarter-of-year effects: each quarter vs the same quarter of 2022
            for k, d in D.items():
                if k[0] != m or k[3] != stratum or d[0] < a.min_base or k[4] < a.from_year: continue
                v = vexp(k)
                if v is None: continue
                rows.append(k); w.append(d[0]); ycol.append(100 * (d[1] - d[2]) / d[0])
                X.append([((v - mu) / sd) * (1.0 if (k[4], k[5]) == qq else 0.0) for qq in qs if qq not in refs])
            if len(rows) < 30: continue
            qlist = [qq for qq in qs if qq not in refs]
            wv = np.array(w); c1, g1 = codes([(r[1], r[2], r[5]) if a.fe == 'cellqoy' else (r[1], r[2]) for r in rows]); c2, g2 = codes([(r[1], r[4], r[5]) + ((priors.bds_sector(r[2]),) if a.time_fe == 'countysector' else ()) for r in rows])
            Z = demean(np.column_stack([ycol, np.array(X)]), wv, [(c1, g1), (c2, g2)])
            cl, G = codes([ind_key(r[2]) for r in rows])
            res = fit(Z[:, 0], Z[:, 1:], wv, cl, G, None, boot=0)
            if res:
                for j, qq in enumerate(qlist): es_rows.append({'panel': lab, 'stratum': stratum, 'quarter': f'{qq[0]}Q{qq[1]}', 'beta': float(res['b'][j]), 'se': float(res['se'][j])})
    with open(os.path.join(a.out, 's3_estimates.csv'), 'w', newline='') as fh:
        wr = csv.DictWriter(fh, list(est_rows[0])); wr.writeheader(); wr.writerows(est_rows)
    if es_rows:
        with open(os.path.join(a.out, 's3_event_study.csv'), 'w', newline='') as fh:
            wr = csv.DictWriter(fh, list(es_rows[0])); wr.writeheader(); wr.writerows(es_rows)
        L.append('\nEvent study, net change, version 1 (exposure x quarter, jointly estimated; reference: ' + ('the same quarter of 2022' if a.fe == 'cellqoy' else '2022 Q4') + '; coefficient (SE)):')
        for lab in {r['panel'] for r in es_rows}:
            for stratum in order:
                rs = [r for r in es_rows if r['panel'] == lab and r['stratum'] == stratum]
                if rs: L.append(f'  {lab} {stratum}: ' + ' '.join(f'{r["quarter"]} {r["beta"]:+.2f}({r["se"]:.2f})' for r in rs))
    open(os.path.join(a.out, 's3_report.txt'), 'w').write('\n'.join(L) + '\n'); print('\n'.join(L))


if __name__ == '__main__':
    main()
