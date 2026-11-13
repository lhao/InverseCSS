"""Step 3a: create synthetic establishments per county x NAICS 6-digit x private x quarter (random baseline).

Random baseline = maximum entropy given the published margins (SYNTHETIC_PANEL_DESIGN section 4):
  count          N = QCEW establishments of the cell (always published; converted 2021 cells rounded stochastically)
  size class     N establishments split over CBP employment-size classes by a multinomial draw with CBP's class
                 shares for the county x 6-digit cell (CBP year = data year; 2024 uses CBP 2023), then the minimal
                 adjustment (one class down / up at a time) that makes the classes consistent with QCEW employment
                 (CBP and QCEW differ in source and reference month; seasonal cells can have zero employment, C8). CBP classes shown
                 as 'N' (not available) share the residual (est - sum of published classes) equally (DATA_CONDITIONS C8).
                 Fallback when the cell has no CBP row: CBP shares of the county x 5-, 4-digit parent, then statewide.
  size           uniform within the class bounds (maximum entropy on a bounded range), then adjusted within bounds
                 so the sizes add up exactly to the cell's QCEW average monthly employment (published or version m)
  monthly emp    size x (QCEW month j / cell average): each month adds up exactly
  wages          proportional to size (equal average wage within the cell): add up to QCEW total quarterly wages
  firm size      QWI national-firm size class (0-19, 20-49, 50-249, 250-499, 500+), drawn independently of firm age;
                 only classes at least as large as the establishment are feasible; class probabilities are raked
                 (iteratively) so the cell's expected employment shares equal the QWI state 6-digit shares (R7)
  firm age       QWI firm age class (0-1, 2-3, 4-5, 6-10, 11+ years), drawn independently of firm size, raked likewise
Draws use fixed uniforms per establishment (reproducible, conditions.rng_for), so raking never changes the random
numbers, only the probabilities.

Outputs (<out_dir>): establishments_<tag>.csv.gz (one row per version x quarter x establishment), report_<tag>.txt
Usage: python3 create_establishments.py <qcew_raw_root> <qcew_imputed_dir> <qcew_complete_dir> <cbp_dir> <qwi_dir> <out_dir>
                                        [--only 24047:7211,24047:7225,24031:5417] [--draws 5] [--seed 11] [--tag slices]
"""
import argparse, collections, csv, gzip, io, math, os, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

# CBP classes are defined on whole-number March employment; average monthly employment is continuous, so the
# bounds are continuous: [0, 5), [5, 10), ... (upper bound = next class's lower bound)
CBP_CLASSES = [('n<5', 0, 5), ('n5_9', 5, 10), ('n10_19', 10, 20), ('n20_49', 20, 50), ('n50_99', 50, 100),
               ('n100_249', 100, 250), ('n250_499', 250, 500), ('n500_999', 500, 1000), ('n1000', 1000, 20000)]
FIRM_SIZE = [('1', 0, 19), ('2', 20, 49), ('3', 50, 249), ('4', 250, 499), ('5', 500, float('inf'))]
FIRM_AGE = ['1', '2', '3', '4', '5']
MEAS = ('qtrly_estabs', 'month1_emplvl', 'month2_emplvl', 'month3_emplvl', 'total_qtrly_wages')


def load_qcew(raw, imp, complete, draws, keep):
    """{(y, q): {m: {(county, naics6): (estabs, m1, m2, m3, wages)}}} for private 6-digit county cells kept by `keep`.
    `draws`: number of versions (1..draws) or a list of versions (F13)."""
    out = {}
    vs = list(range(1, draws + 1)) if isinstance(draws, int) else list(draws)
    for y, q, qdir in cond.quarter_dirs(raw):
        cp = os.path.join(complete, f'qcew_complete_{y}q{q}_M20.csv.gz')
        per = {m: {} for m in vs}
        if os.path.exists(cp):
            with cond.open_text(cp) as fh:
                for r in csv.DictReader(fh):
                    m = int(r['draw'])
                    if m in per and r['own_code'] == '5' and r['level'] == '6' and r['area_fips'] != '24000' and keep(r['area_fips'], r['industry_code']):
                        per[m][(r['area_fips'], r['industry_code'])] = tuple(float(r[v]) for v in MEAS)
        else:
            pub = {}
            for f in cond.area_files(qdir):
                with cond.open_text(f) as fh:
                    for r in csv.DictReader(fh):
                        if r['agglvl_code'] == '78' and r['own_code'] == '5' and keep(r['area_fips'], r['industry_code']):
                            if cond.qcew_status(r) in ('published', 'true_zero'):
                                pub[(r['area_fips'], r['industry_code'])] = tuple(float(r[v]) if v == 'qtrly_estabs' else cond.qcew_value(r, v) for v in MEAS)
            for m in per: per[m].update(pub)
            with cond.open_text(os.path.join(imp, f'qcew_imputed_{y}q{q}_M20.csv.gz')) as fh:
                for r in csv.DictReader(fh):
                    m = int(r['draw'])
                    if m in per and r['own_code'] == '5' and r['level'] == '6' and keep(r['area_fips'], r['industry_code']):
                        per[m][(r['area_fips'], r['industry_code'])] = tuple(float(r[v]) for v in MEAS)
        out[(y, q)] = per
    return out


def load_cbp(cbp_dir, keep_county):
    """{year: {(county, code): {class: count or None}}} for Maryland county rows; code is the NAICS code without '/'."""
    out = {}
    for y in (2021, 2022, 2023):
        z = zipfile.ZipFile(cond.require(os.path.join(cbp_dir, f'cbp{str(y)[2:]}co.zip')))
        d = {}
        with z.open(z.namelist()[0]) as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh, encoding='latin-1')):
                if r['fipstate'].strip() != '24': continue
                c = '24' + r['fipscty'].strip()
                code = r['naics'].strip().rstrip('/').rstrip('-')
                if not code: code = 'total'
                d[(c, code)] = {'est': float(r['est']), **{k: (None if r[k].strip() == 'N' else float(r[k])) for k, _, _ in CBP_CLASSES}}
        out[y] = d
    return out


def class_shares(cbp, county, naics):
    """CBP class shares for the cell; 'N' classes share the residual equally. Falls back to parents, then statewide."""
    for code, src in ((naics, '6-digit'), (naics[:5], '5-digit'), (naics[:4], '4-digit')):
        r = cbp.get((county, code))
        if r and r['est'] > 0:
            pub = sum(v for k, v in r.items() if k != 'est' and v is not None)
            nn = [k for k, _, _ in CBP_CLASSES if r[k] is None]
            resid = max(r['est'] - pub, 0.0)
            cnt = {k: (r[k] if r[k] is not None else (resid / len(nn) if nn else 0.0)) for k, _, _ in CBP_CLASSES}
            tot = sum(cnt.values())
            if tot > 0: return {k: v / tot for k, v in cnt.items()}, src
    st = collections.Counter()
    for (c, code), r in cbp.items():
        if code == naics:
            for k, _, _ in CBP_CLASSES:
                if r[k] is not None: st[k] += r[k]
    tot = sum(st.values())
    if tot > 0: return {k: st[k] / tot for k, _, _ in CBP_CLASSES}, 'statewide'
    return None, 'none'


def fit_sizes(lo, hi, u, total):
    """Sizes uniform within bounds (u = fixed uniforms), adjusted within bounds to add up to `total`."""
    s = [l + ui * (h - l) for l, h, ui in zip(lo, hi, u)]
    if sum(lo) > total or sum(hi) < total: return None
    for _ in range(200):
        gap = total - sum(s)
        if abs(gap) < 1e-6: break
        room = [(h - x) if gap > 0 else (x - l) for x, l, h in zip(s, lo, hi)]
        rt = sum(room)
        if rt <= 0: break
        s = [x + gap * r / rt for x, r in zip(s, room)]
        s = [min(max(x, l), h) for x, l, h in zip(s, lo, hi)]
    return s


def rake_assign(sizes, feasible, target, u, iters=60):
    """Probabilities p[e][k] proportional to w_k on feasible classes, w raked so expected employment shares equal
    `target`; then each establishment draws its class with its fixed uniform u[e]."""
    K = list(target)
    w = {k: max(target[k], 1e-9) for k in K}
    tot = sum(sizes) or 1.0
    for _ in range(iters):
        exp = collections.Counter()
        for s, f in zip(sizes, feasible):
            z = sum(w[k] for k in f)
            for k in f: exp[k] += s * w[k] / z
        for k in K:
            if exp[k] > 0 and target[k] > 0: w[k] *= (target[k] * tot / exp[k]) ** 0.5
    out = []
    for s, f, ui in zip(sizes, feasible, u):
        z = sum(w[k] for k in f); acc = 0.0; pick = f[-1]
        for k in f:
            acc += w[k] / z
            if ui <= acc: pick = k; break
        out.append(pick)
    return out


def qwi_margins(path, dim):
    """{(naics6, y, q): {class: share of Emp}} from QWI state x 6-digit by firm size or firm age (private, all workers)."""
    out = collections.defaultdict(dict)
    with cond.open_text(path) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] == 'S' and r['ind_level'] == '6' and r['ownercode'] == 'A05' and r['sex'] == '0' and r['agegrp'] == 'A00' and r[dim] != '0':
                v = cond.qwi_value(r, 'Emp')
                if v is not None: out[(r['industry'], int(r['year']), int(r['quarter']))][r[dim]] = v
    res = {}
    for k, d in out.items():
        t = sum(d.values())
        if t > 0: res[k] = {c: d.get(c, 0.0) / t for c in (FIRM_AGE if dim == 'firmage' else [x[0] for x in FIRM_SIZE])}
    return res


def main():
    ap = argparse.ArgumentParser()
    for x in ('raw', 'imp', 'complete', 'cbp', 'qwi', 'out'): ap.add_argument(x)
    ap.add_argument('--only', default='24047:7211,24047:7225,24031:5417'); ap.add_argument('--draws', type=int, default=5)
    ap.add_argument('--seed', type=int, default=11); ap.add_argument('--tag', default='slices')
    ap.add_argument('--versions', default='', help="'' = 1..draws; 'A-B' = only these versions (one Slurm array task per version; merge_versions.py)")
    ap.add_argument('--prior', default='random', choices=('random', 'systematic'),
                    help='systematic: firm age x size drawn jointly from the BDS national sector table, raked to both QWI margins (step 3d)')
    ap.add_argument('--county-size-cal', default='data/processed/county_size_calibration/county_size_calibration.csv',
                    help="D11: transfer variances from calibrate_county_size.py; '' = state 6-digit firm-size margins only")
    ap.add_argument('--bds-joint', default='data/raw/external/bds/bds2023_sec_fa_fz_2019-2023.csv.gz')
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])  # F11
    only = [tuple(x.split(':')) for x in a.only.split(',') if x]
    keep = (lambda c, n: any(c == oc and n.startswith(op) for oc, op in only)) if only else (lambda c, n: True)
    V = cond.parse_versions(a.versions, a.draws)
    qcew = load_qcew(a.raw, a.imp, a.complete, V, keep)
    cbp = load_cbp(a.cbp, None)
    fsz = qwi_margins(os.path.join(a.qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'), 'firmsize')
    # D11: firm-size targets consistent with QWI county x sector shares (reliability-weighted), unless disabled
    CST = {}
    if a.county_size_cal:
        cs_keep = {(c, priors.bds_sector(p_)) for c, p_ in only}
        qcew_sector = load_qcew(a.raw, a.imp, a.complete, V, (lambda c, n_: (c, priors.bds_sector(n_)) in cs_keep) if only else (lambda c, n_: True))
        CST = priors.county_size_targets(qcew_sector, fsz, priors.county_sector_size(os.path.join(a.qwi, 'qwi_md_sa_fs_gc_ns_op_u_2021-2024.csv.gz')),
                                         priors.load_transfer_var(a.county_size_cal))
    fag = qwi_margins(os.path.join(a.qwi, 'qwi_md_sa_fa_gs_n6_op_u_2021-2024.csv.gz'), 'firmage')
    joint = priors.joint(a.bds_joint) if a.prior == 'systematic' else None
    log = collections.Counter(); checks = []
    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, f'establishments_{a.tag}.csv.gz.part')
    with gzip.open(tmp, 'wt', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'est_id', 'cbp_size_class', 'emp_avg', 'emp_m1', 'emp_m2', 'emp_m3',
                    'wages', 'firm_size_class', 'firm_age_class'])
        for (y, q), per in sorted(qcew.items()):
            cb = cbp[min(y, 2023)]
            for m, cells in per.items():
                for (c, n), (est, m1, m2, m3, wages) in sorted(cells.items()):
                    rng = cond.rng_for(a.seed, m, c, n, y, q)
                    N = int(est) + (1 if rng.random() < est - int(est) else 0)
                    if N <= 0: continue
                    T = (m1 + m2 + m3) / 3
                    shares, src = class_shares(cb, c, n)
                    log[f'cbp_source_{src}'] += 1
                    if shares is None: shares = {CBP_CLASSES[0][0]: 1.0}
                    # multinomial draw with CBP shares, then the minimal adjustment that makes the classes consistent
                    # with QCEW employment: move establishments one class down (largest first) or up (smallest first)
                    uu = [rng.random() for _ in range(N)]
                    order = [k for k, _, _ in CBP_CLASSES]; bounds = {k: (l, h) for k, l, h in CBP_CLASSES}
                    ci = [order.index(next((k for k, p in _cum(shares) if ui <= p), order[-1])) for ui in uu]
                    moves = 0
                    while sum(bounds[order[i]][0] for i in ci) > T and any(i > 0 for i in ci):
                        j = max(range(N), key=lambda e: (ci[e], uu[e])); ci[j] -= 1; moves += 1
                    while sum(bounds[order[i]][1] for i in ci) < T and any(i < len(order) - 1 for i in ci):
                        j = min(range(N), key=lambda e: (ci[e], uu[e])); ci[j] += 1; moves += 1
                    classes = [order[i] for i in ci]
                    lo = [bounds[k][0] for k in classes]; hi = [bounds[k][1] for k in classes]
                    relaxed = not (sum(lo) <= T <= sum(hi))
                    if moves: log['class_moves'] += moves; log['cells_with_class_moves'] += 1
                    if relaxed:
                        log['size_classes_relaxed'] += 1; lo = [0.0] * N; hi = [max(T, 1.0)] * N
                    us = [rng.random() for _ in range(N)]
                    sizes = fit_sizes(lo, hi, us, T)
                    if sizes is None: sizes = [T / N] * N; log['sizes_equal_fallback'] += 1
                    fs_t = CST.get((m, c, n, y, q)) or fsz.get((n, y, q)) or fsz.get((n, y, max(q - 1, 1)))
                    fa_t = fag.get((n, y, q)) or fag.get((n, y, max(q - 1, 1)))
                    if fs_t is None: fs_t = {k: 0.2 for k, _, _ in FIRM_SIZE}; log['firm_size_margin_missing'] += 1
                    if fa_t is None: fa_t = {k: 0.2 for k in FIRM_AGE}; log['firm_age_margin_missing'] += 1
                    feas = [[k for k, lo_, hi_ in FIRM_SIZE if hi_ >= s] for s in sizes]
                    if joint is None:
                        fs = rake_assign(sizes, feas, fs_t, [rng.random() for _ in range(N)])
                        fa = rake_assign(sizes, [FIRM_AGE] * N, fa_t, [rng.random() for _ in range(N)])
                    else:  # systematic: joint draw from the BDS age x size table, raked to both margins
                        pr = joint.get((priors.bds_sector(n), min(y, 2023))) or {(x, z): 1.0 for x in priors.AGES for z in priors.SIZES}
                        pairs = priors.rake_joint(sizes, feas, fa_t, fs_t, pr, [rng.random() for _ in range(N)])
                        fa = [p_[0] for p_ in pairs]; fs = [p_[1] for p_ in pairs]
                    for i in range(N):
                        f = sizes[i] / T if T > 0 else 1 / N
                        w.writerow([m, y, q, c, n, f'{c}-{n}-{y}q{q}-{i + 1:04d}', classes[i], cond.out_value(sizes[i]),
                                    cond.out_value(m1 * f), cond.out_value(m2 * f), cond.out_value(m3 * f), cond.out_value(wages * f), fs[i], fa[i]])
                    # checks
                    ach_fs = collections.Counter(); ach_fa = collections.Counter()
                    for s, k1, k2 in zip(sizes, fs, fa): ach_fs[k1] += s; ach_fa[k2] += s
                    young = sum(s for s, k1, k2 in zip(sizes, fs, fa) if k2 in ('1', '2'))
                    young_small = sum(s for s, k1, k2 in zip(sizes, fs, fa) if k2 in ('1', '2') and k1 == '1')
                    checks.append({'version': m, 'year': y, 'quarter': q, 'county': c, 'naics6': n, 'N': N, 'T': T,
                                   'sum_sizes_minus_T': sum(sizes) - T,
                                   'fs_gap': sum(abs(ach_fs[k] / T - fs_t[k]) for k in fs_t) / 2 if T > 0 else 0,
                                   'fa_gap': sum(abs(ach_fa[k] / T - fa_t[k]) for k in fa_t) / 2 if T > 0 else 0,
                                   'young_small': young_small / young if young > 0 else None, 'young_emp': young,
                                   'cbp_src': src, 'fs_infeasible': sum(1 for s, f in zip(sizes, feas) if not f), 'relaxed': relaxed,
                                   'cbp_est': (cb.get((c, n)) or {}).get('est'), 'cbp_emp_range': (sum(l for l in lo), sum(h for h in hi)) if not relaxed else None})
                    log['establishments'] += N; log['cells'] += 1
    os.replace(tmp, os.path.join(a.out, f'establishments_{a.tag}.csv.gz'))
    save_diag(a, V, log, checks)
    report(a, V, log, checks)


DIAG_COLS = ('version', 'year', 'quarter', 'county', 'naics6', 'N', 'T', 'sum_sizes_minus_T', 'fs_gap', 'fa_gap', 'young_small',
             'young_emp', 'cbp_src', 'relaxed', 'cbp_est')


def save_diag(a, V, log, checks):
    """Per-cell checks and the log, so merge_versions.py can rebuild the report over all versions (F13)."""
    import json
    with gzip.open(os.path.join(a.out, f'diag_{a.tag}.json.gz'), 'wt') as fh:
        json.dump({'args': vars(a), 'versions': V, 'log': dict(log), 'run': cond.run_line(),
                   'checks': [[x[k] for k in DIAG_COLS] for x in checks]}, fh)


def load_diag(path):
    import json
    d = json.load(gzip.open(path, 'rt'))
    d['checks'] = [dict(zip(DIAG_COLS, r)) for r in d['checks']]
    return d


def report(a, V, log, checks, run=None):
    import statistics as st
    big = [x for x in checks if x['T'] >= 50]
    lines = [f'Step 3a ({a.prior}): {log["cells"]:,} cell-quarter-versions, {log["establishments"]:,} establishments (versions {cond.versions_label(V)}); cells {a.only}',
             f'log: {dict(log)}',
             f'max |sum of sizes - QCEW employment|: {max(abs(x["sum_sizes_minus_T"]) for x in checks):.2e}',
             f'firm-size employment shares vs firm-size target (QWI state 6-digit; county-adjusted when D11 is on), total variation distance: median {st.median(x["fs_gap"] for x in big):.3f}, '
             f'90th pct {sorted(x["fs_gap"] for x in big)[int(.9 * len(big))]:.3f} (cells >= 50 jobs, n={len(big)})',
             f'firm-age employment shares vs QWI margin, total variation distance: median {st.median(x["fa_gap"] for x in big):.3f}, '
             f'90th pct {sorted(x["fa_gap"] for x in big)[int(.9 * len(big))]:.3f}',
             f'prior: {a.prior}; age x size association: share of young-firm (0-3 years) employment in firms of 0-19, '
             f'employment-weighted over cells: {sum(x["young_small"] * x["young_emp"] for x in checks if x["young_small"] is not None) / max(sum(x["young_emp"] for x in checks if x["young_small"] is not None), 1e-9):.3f}',
             'by cell (version 1, 2023 Q2): N establishments, employment, firm-size TVD, firm-age TVD, CBP source']
    for x in checks:
        if x['version'] == 1 and (x['year'], x['quarter']) == (2023, 2):
            lines.append(f'  {x["county"]} {x["naics6"]}: N={x["N"]:4d} emp={x["T"]:8.1f} fsTVD={x["fs_gap"]:.3f} faTVD={x["fa_gap"]:.3f} cbp={x["cbp_src"]}')
    rel = collections.Counter((x['county'], x['naics6']) for x in checks if x['relaxed'])
    lines.append('size classes relaxed (cell: count of quarter-versions; QCEW N and employment vs CBP establishments, version 1 example):')
    for (c, n), k in rel.most_common(12):
        ex = next(x for x in checks if x['relaxed'] and (x['county'], x['naics6']) == (c, n))
        lines.append(f'  {c} {n}: {k}   e.g. {ex["year"]}Q{ex["quarter"]}: QCEW N={ex["N"]}, QCEW emp={ex["T"]:.1f}, CBP est={ex["cbp_est"]}, source={ex["cbp_src"]}')
    lines.append(run or cond.run_line())
    print('\n'.join(lines))
    open(os.path.join(a.out, f'report_{a.tag}.txt'), 'w').write('\n'.join(lines) + '\n')


def _cum(shares):
    acc = 0.0
    for k, _, _ in CBP_CLASSES:
        acc += shares.get(k, 0.0); yield k, acc


if __name__ == '__main__':
    main()
