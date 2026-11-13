"""D11: calibrate how much to trust QWI county x sector firm-size shares relative to the state 6-digit margins.

For every Maryland county x sector x quarter (private), compare
  implied  firm-size class employment if each 6-digit industry in the county had its STATE 6-digit firm-size shares:
           I_k = sum_n E_n x s_state(n, k), with E_n the QCEW county 6-digit employment (published or imputed, version 1)
  observed QWI county x sector by firm size, (Emp + EmpEnd) / 2, published classes only
The log ratio r_k = log(observed share / implied share) mixes two errors:
  noise     QWI noise infusion; sigma by class-cell size from the QWI / QCEW spread (conditions.SIGMA_STOCK). That spread
            also includes concept differences, so it is an UPPER bound on noise, and the weights below are conservative
  transfer  the county really differs from the state (e.g. local vs chain firms, D11)
By size band of the observed class cell: transfer variance = max(var(r) - mean noise variance, 0). The weight on the
county value is w = transfer var / (transfer var + noise var(cell)); w is reported, and a sensitivity with noise halved.

Outputs (<out_dir>): county_size_calibration.csv (size band, n, var(r), noise var, transfer var, mean w),
county_size_residuals.csv.gz (one row per county x sector x quarter x class), calibration_report.txt
Usage: python3 calibrate_county_size.py <qcew_raw_root> <qcew_imputed_dir> <qcew_complete_dir> <qwi_dir> <out_dir>
"""
import argparse, collections, csv, gzip, math, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import create_establishments as ce
import priors

from priors import BANDS, band, county_sector_size, implied, blend


def main():
    ap = argparse.ArgumentParser()
    for x in ('raw', 'imp', 'complete', 'qwi', 'out'): ap.add_argument(x)
    a = ap.parse_args()
    cond.start_run(a.raw, a.imp, a.complete, a.qwi)
    obs = county_sector_size(os.path.join(a.qwi, 'qwi_md_sa_fs_gc_ns_op_u_2021-2024.csv.gz'))
    fsz = ce.qwi_margins(os.path.join(a.qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'), 'firmsize')
    qcew = ce.load_qcew(a.raw, a.imp, a.complete, 1, lambda c, n: True)
    res = []
    for (y, q), per in sorted(qcew.items()):
        by_cs = collections.defaultdict(dict)
        for (c, n), (est, m1, m2, m3, w) in per[1].items():
            by_cs[(c, priors.bds_sector(n))][n] = (m1 + m2 + m3) / 3
        for (c, sec), cells in by_cs.items():
            O = obs.get((c, sec, y, q))
            if not O: continue
            I, cov = implied(cells, fsz, y, q)
            if cov < 0.8 or sum(I.values()) <= 0: continue
            P = [k for k in priors.SIZES if k in O and O[k] > 0 and I.get(k, 0) > 0]
            if len(P) < 2: continue
            to = sum(O[k] for k in P); ti = sum(I[k] for k in P)
            for k in P:
                res.append({'county': c, 'sector': sec, 'year': y, 'quarter': q, 'class': k, 'obs_emp': O[k],
                            'obs_share': O[k] / to, 'implied_share': I[k] / ti, 'r': math.log((O[k] / to) / (I[k] / ti)),
                            'noise_var': cond.sigma(cond.SIGMA_STOCK, O[k]) ** 2, 'coverage': cov})
    os.makedirs(a.out, exist_ok=True)
    with gzip.open(os.path.join(a.out, 'county_size_residuals.csv.gz'), 'wt', newline='') as fh:
        w = csv.DictWriter(fh, list(res[0])); w.writeheader(); w.writerows(res)
    cal = []
    L = ['D11 calibration: QWI county x sector firm-size shares vs state-implied shares, Maryland private, 2021-2024',
         f'{len(res):,} county x sector x quarter x class comparisons (coverage of state 6-digit margins >= 80%)', '',
         f'{"class-cell size":>16s} {"n":>7s} {"var(r)":>8s} {"noise var":>10s} {"transfer var":>13s} {"mean w":>7s} {"mean w, noise/2":>16s}']
    tv = {}
    for _, b in BANDS:
        rs = [x for x in res if band(x['obs_emp']) == b]
        if len(rs) < 30: continue
        v = st.pvariance([x['r'] for x in rs]); nv = st.mean(x['noise_var'] for x in rs); t = max(v - nv, 0.0); tv[b] = t
        wm = st.mean(t / (t + x['noise_var']) if t + x['noise_var'] > 0 else 0 for x in rs)
        wh = st.mean(t / (t + x['noise_var'] / 4) if t + x['noise_var'] > 0 else 0 for x in rs)
        cal.append({'band': b, 'n': len(rs), 'var_r': v, 'noise_var': nv, 'transfer_var': t, 'mean_w': wm, 'mean_w_noise_half': wh})
        L.append(f'{b:>16s} {len(rs):7,d} {v:8.3f} {nv:10.3f} {t:13.3f} {wm:7.2f} {wh:16.2f}')
    with open(os.path.join(a.out, 'county_size_calibration.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, list(cal[0])); w.writeheader(); w.writerows(cal)
    # how far the county tables move the shares: blended vs implied, for the pilot counties
    L += ['', 'Example: Worcester (24047) sector 72 and Montgomery (24031) sector 54, 2023 Q2 (shares of employment by firm size 0-19 ... 500+):']
    wfun = lambda k, ok: (lambda t, nv: t / (t + nv) if t + nv > 0 else 0)(tv.get(band(ok), 0.0), cond.sigma(cond.SIGMA_STOCK, ok) ** 2)
    for c, sec in (('24047', '72'), ('24031', '54')):
        cells = {n: (m1 + m2 + m3) / 3 for (cc, n), (est, m1, m2, m3, w_) in qcew[(2023, 2)][1].items() if cc == c and priors.bds_sector(n) == sec}
        I, cov = implied(cells, fsz, 2023, 2); O = obs.get((c, sec, 2023, 2), {})
        B = blend(I, O, wfun); ti = sum(I.values()); to = sum(O.values())
        L.append(f'  {c} {sec}: state-implied ' + ' '.join(f'{I.get(k, 0) / ti:.2f}' for k in priors.SIZES) +
                 ' | QWI county ' + ' '.join(f'{O.get(k, 0) / to:.2f}' if k in O else '  - ' for k in priors.SIZES) +
                 ' | blended ' + ' '.join(f'{B[k]:.2f}' for k in priors.SIZES) + f'  (coverage {cov:.0%})')
    open(os.path.join(a.out, 'calibration_report.txt'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
