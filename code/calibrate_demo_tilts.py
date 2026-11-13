"""D13: how much of a county's firm-size pattern in worker composition does the STATE pattern predict?

Worker composition by firm size enters the worker groups as a tilt: state share of group g in firm-size class k
relative to the state all-size share. D13 found this transfers unevenly to counties, so the tilt is shrunk:
tilt^lambda_k. lambda_k is estimated from QWI county x sector x firm size x sex x age (all Maryland counties EXCEPT the
held-out ones, default the pilot counties), by weighted regression through the origin of
    y = log(county share of g in class k / county all-size share of g)     (observed county tilt)
on  x = log(state share of g in class k / state all-size share of g)      (state tilt, same sector)
with weights 1 / noise variance (QWI noise by cell size, conditions.SIGMA_STOCK: an upper bound, see D11).
lambda = 1: the state pattern transfers fully; 0: not at all.

Outputs (<out_dir>): demo_tilt_lambda.csv (firm size class, lambda, n, weighted R2), report
Usage: python3 calibrate_demo_tilts.py <qwi_dir> <out_dir> [--exclude 24047,24031]
"""
import argparse, collections, csv, math, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

SEX = ['1', '2']; AGES = ['A01', 'A02', 'A03', 'A04', 'A05', 'A06', 'A07', 'A08']


def load(path, geo, sectorise):
    out = collections.defaultdict(lambda: collections.defaultdict(float))
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != geo or r['ownercode'] != 'A05' or r['sex'] not in SEX or r['agegrp'] not in AGES: continue
            if geo == 'S' and r['ind_level'] != '6': continue
            v0, v1 = cond.qwi_value(r, 'Emp'), cond.qwi_value(r, 'EmpEnd')
            if v0 is None or v1 is None: continue
            ind = priors.bds_sector(r['industry']) if sectorise else r['industry']
            out[(r['geography'] if geo == 'C' else 'S', ind, int(r['year']), int(r['quarter']), r['firmsize'])][(r['sex'], r['agegrp'])] += (v0 + v1) / 2
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('qwi'); ap.add_argument('out'); ap.add_argument('--exclude', default='24047,24031')
    a = ap.parse_args()
    cond.start_run(a.qwi)
    excl = set(a.exclude.split(','))
    cty = load(os.path.join(a.qwi, 'qwi_md_sa_fs_gc_ns_op_u_2021-2024.csv.gz'), 'C', False)
    stt = load(os.path.join(a.qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'), 'S', True)   # state 6-digit summed to sector
    acc = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])   # class -> [sum wxy, sum wxx, sum wyy, sum w, n]
    for (c, sec, y, q, k), d in cty.items():
        if k == '0' or c in excl: continue
        d0 = cty.get((c, sec, y, q, '0')); s_k = stt.get(('S', sec, y, q, k)); s_0 = stt.get(('S', sec, y, q, '0'))
        if not d0 or not s_k or not s_0: continue
        tk, t0, sk, s0 = sum(d.values()), sum(d0.values()), sum(s_k.values()), sum(s_0.values())
        if min(tk, t0, sk, s0) <= 0: continue
        for g, v in d.items():
            if v <= 0 or d0.get(g, 0) <= 0 or s_k.get(g, 0) <= 0 or s_0.get(g, 0) <= 0: continue
            yv = math.log((v / tk) / (d0[g] / t0)); xv = math.log((s_k[g] / sk) / (s_0[g] / s0))
            w = 1.0 / (cond.sigma(cond.SIGMA_STOCK, v) ** 2 + cond.sigma(cond.SIGMA_STOCK, d0[g]) ** 2)
            A = acc[k]; A[0] += w * xv * yv; A[1] += w * xv * xv; A[2] += w * yv * yv; A[3] += w; A[4] += 1
    rows = []
    L = ['D13: shrinkage of state firm-size tilts in worker composition (sex x age), estimated on Maryland counties',
         f'excluding the held-out counties {sorted(excl)}; weighted regression through the origin of county tilt on state tilt', '',
         f'{"firm size":>10s} {"lambda":>7s} {"weighted R2":>12s} {"n":>8s}']
    for k in priors.SIZES:
        A = acc[k]
        if A[1] <= 0: continue
        lam = A[0] / A[1]; r2 = (A[0] ** 2 / A[1]) / A[2] if A[2] > 0 else 0.0
        lam_b = min(max(lam, 0.0), 1.0)
        rows.append({'firm_size_class': k, 'lambda': lam_b, 'lambda_unbounded': lam, 'weighted_r2': r2, 'n': A[4]})
        L.append(f'{k:>10s} {lam_b:7.2f} {r2:12.2f} {A[4]:8,d}' + ('' if lam_b == lam else f'   (estimate {lam:.2f}, bounded to [0, 1])'))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, 'demo_tilt_lambda.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, list(rows[0])); w.writeheader(); w.writerows(rows)
    open(os.path.join(a.out, 'demo_tilt_report.txt'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
