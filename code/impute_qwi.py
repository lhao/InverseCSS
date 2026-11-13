"""Multiple imputation of suppressed QWI county cells (private, all workers): county total -> sector -> 3-digit -> 4-digit.

In every draw m, top-down by level:
  - children with all core indicators published are fixed (known);
  - leftover = parent - sum(known children), for every indicator (stocks, hires, separations add up within +-2:
    DATA_CONDITIONS B8; job gains/losses only approximately, children >= parent: B9), made exactly consistent (B1);
  - the leftover is split among the suppressed children with the soft allocation of downscale_qwi.allocate
    (QCEW county weights of the child level, published or imputed draw m; statewide QWI rates of the child industry;
    identities and bounds enforced exactly);
  - imputed children become the parents of the next level.
A single suppressed child receives the leftover exactly.

Validation (--validate F): additionally hide a share F of published 4-digit cells (Emp <= --hide-max-emp) that have at
least one suppressed sibling, impute them, and score against the truth (allocation vs a proportional baseline).

Output: data/processed/qwi_imputed/qwi_imputed_county_M<M>.csv.gz with columns
  draw, area_fips, industry_code, ind_level, year, quarter, <indicators>   (suppressed cells only)

Usage:
  python3 impute_qwi.py <qwi_dir> <qcew_raw_root> <qcew_imputed_dir> <out_dir> [--draws M] [--seed S] [--validate F]
                        [--qcew-complete <folder of NAICS-2022 converted quarters>]
"""
import argparse, collections, csv, gzip, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import downscale_qwi as ds

VARS = ds.VARS
CORE = ds.NEED                     # Emp, EmpEnd, EmpS, HirA, Sep, FrmJbGn, FrmJbLs, EarnS
OPTIONAL = ds.OPTIONAL             # HirN, HirR, HirAEnd, SepBeg
LEVELS = ('S', '3', '4')
QCEW_LEVEL = {'S': '2', '3': '3', '4': '4'}
STATE = '24000'


def qwi_parent(code, lvl):
    """QWI industry one level up; the county total is '00' in QWI (QCEW uses '10')."""
    if lvl == 'S': return '00'
    if lvl == '3': return cond.SECTOR.get(code[:2], code[:2])
    return code[:3]


def values(row):
    """Core + optional indicators of a QWI row if all core ones are published, else None."""
    pv = {v: cond.qwi_value(row, v) for v in CORE}
    if None in pv.values(): return None
    for v in OPTIONAL: pv[v] = cond.qwi_value(row, v)
    if pv['HirN'] is None or pv['HirR'] is None: pv['HirN'] = pv['HirR'] = None
    if pv['HirAEnd'] is None or pv['SepBeg'] is None: pv['HirAEnd'] = pv['SepBeg'] = None
    cond.make_consistent(pv)
    return pv


def leftover(P, known):
    """Parent minus known children, made consistent. EarnS is carried as stable payroll (EarnS x EmpS)."""
    L = {}
    for v in ('Emp', 'EmpEnd', 'EmpS', 'HirA', 'FrmJbGn'):
        L[v] = max(P[v] - sum(k[v] for k in known), 0.0)
    pay = P['EarnS'] * P['EmpS'] - sum(k['EarnS'] * k['EmpS'] for k in known)
    L['Sep'] = L['FrmJbLs'] = 0.0
    for a, b in (('HirN', 'HirR'), ('HirAEnd', 'SepBeg')):
        if P.get(a) is not None and all(k.get(a) is not None for k in known):
            L[a] = max(P[a] - sum(k[a] for k in known), 0.0); L[b] = 0.0
        else:
            L[a] = L[b] = None
    cond.make_consistent(L)
    L['EarnS'] = pay / L['EmpS'] if L['EmpS'] > 0 and pay > 0 else P['EarnS']
    return L


def main():
    ap = argparse.ArgumentParser()
    for x in ('qwi_dir', 'qcew_raw', 'qcew_imp', 'out'): ap.add_argument(x)
    ap.add_argument('--draws', type=int, default=20); ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--validate', type=float, default=0); ap.add_argument('--hide-max-emp', type=float, default=200)
    ap.add_argument('--sigma-flow', type=float, default=0.75); ap.add_argument('--sigma-earn', type=float, default=0.3)
    ap.add_argument('--earn-size-c', type=float, default=0.0, help='earnings noise multiplier 1 + c/sqrt(EmpS) for small cells')
    ap.add_argument('--start-rule', default='hybrid', choices=cond.START_RULES, help='start-of-quarter weight rule (conditions.start_weight)')
    ap.add_argument('--sigma-net', type=float, default=1.0); ap.add_argument('--stock-scale', type=float, default=1.5)
    ap.add_argument('--trust-growth', type=float, default=0.3)
    ap.add_argument('--qcew-complete', default='', help='folder with NAICS-2022 converted QCEW quarters (convert_qcew_naics.py)')
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])  # F11
    f = lambda tab: os.path.join(a.qwi_dir, f'qwi_md_{tab}_op_u_2021-2024.csv.gz')
    cells = {'A': cond.load_qwi(f('sa_f_gc_n4'), 'C', 'A'), 'S': cond.load_qwi(f('sa_fa_gc_ns'), 'C', 'S'),
             '3': cond.load_qwi(f('sa_f_gc_n3'), 'C', '3'), '4': cond.load_qwi(f('sa_f_gc_n4'), 'C', '4')}
    state = {'S': cond.load_qwi(f('sa_fa_gc_ns'), 'S', 'S'), '3': cond.load_qwi(f('sa_f_gc_n3'), 'S', '3'),
             '4': cond.load_qwi(f('sa_f_gc_n4'), 'S', '4')}
    qcew = {lvl: ds.load_qcew(a.qcew_raw, a.qcew_imp, QCEW_LEVEL[lvl], a.draws, a.qcew_complete) for lvl in LEVELS}
    log = collections.Counter()

    known = {lvl: {k: values(r) for k, r in cells[lvl].items()} for lvl in ('A',) + LEVELS}
    hidden = {}
    if a.validate:
        rng = cond.rng_for(a.seed, 'hide')
        sib = collections.defaultdict(list)
        for k in known['4']: sib[(k[0], qwi_parent(k[1], '4'), k[2], k[3])].append(k)
        for g, ks in sib.items():
            if not any(known['4'][k] is None for k in ks): continue
            for k in ks:
                pv = known['4'][k]
                if pv is not None and 0 < pv['Emp'] <= a.hide_max_emp and rng.random() < a.validate:
                    hidden[k] = pv; known['4'][k] = None
        log['hidden_cells'] = len(hidden)
    for lvl in LEVELS:
        log[f'L{lvl}_suppressed_cells'] = sum(1 for v in known[lvl].values() if v is None)

    def rates(lvl, ind, par, y, q):
        plvl = {'S': None, '3': 'S', '4': '3'}[lvl]
        for src, key in ((state[lvl], (STATE, ind, y, q)), (state.get(plvl, {}), (STATE, par, y, q))):
            r = src.get(key)
            if r and cond.qwi_value(r, 'Emp'):
                e = cond.qwi_value(r, 'Emp'); v = {x: cond.qwi_value(r, x) for x in ('FrmJbGn', 'HirA', 'EmpS', 'EarnS', 'HirN', 'HirAEnd')}
                if None not in (v['FrmJbGn'], v['HirA'], v['EmpS'], v['EarnS']):
                    return {'gain': v['FrmJbGn'] / e, 'churn': max(v['HirA'] - v['FrmJbGn'], 0) / e, 'stable': v['EmpS'] / e, 'earn': v['EarnS'],
                            'newshare': v['HirN'] / v['HirA'] if v['HirN'] is not None and v['HirA'] else 0.7,
                            'endshare': v['HirAEnd'] / v['HirA'] if v['HirAEnd'] is not None and v['HirA'] else 0.6}
        return None

    groups = {lvl: collections.defaultdict(list) for lvl in LEVELS}
    for lvl in LEVELS:
        for k in cells[lvl]: groups[lvl][(k[0], qwi_parent(k[1], lvl), k[2], k[3])].append(k)

    results = collections.defaultdict(list); baseline = {}
    for m in range(1, a.draws + 1):
        val = {lvl: {k: v for k, v in known[lvl].items() if v is not None} for lvl in ('A',) + LEVELS}
        for lvl in LEVELS:
            plvl = {'S': 'A', '3': 'S', '4': '3'}[lvl]
            for (c, par, y, q), ks in groups[lvl].items():
                unk = sorted(k for k in ks if k not in val[lvl])
                if not unk: continue
                P = val[plvl].get((c, par, y, q))
                if P is None:
                    if m == 1: log[f'L{lvl}_parent_unavailable'] += len(unk)
                    continue
                L = leftover(P, [val[lvl][k] for k in ks if k in val[lvl]])
                if L['Emp'] <= 0 and L['EmpEnd'] <= 0:
                    for k in unk: val[lvl][k] = {v: 0.0 for v in VARS if not (v in OPTIONAL and L.get(v) is None)} | {'EarnS': L['EarnS']}
                    if m == 1: log[f'L{lvl}_zero_leftover'] += len(unk)
                    continue
                if len(unk) == 1:
                    val[lvl][unk[0]] = dict(L) | {'FrmJbC': L['EmpEnd'] - L['Emp']}
                    if m == 1: log[f'L{lvl}_single_suppressed'] += 1
                    continue
                cur = qcew[lvl].get((y, q), {}).get(m, {}); py, pq = (y, q - 1) if q > 1 else (y - 1, 4)
                prev = qcew[lvl].get((py, pq), {}).get(m, {})
                start, grow, rt = [], [], []
                for k in unk:
                    ck = (c, k[1])
                    if ck in cur and cur[ck][0] > 0:
                        m1, m3 = cur[ck]; s0, brk = cond.start_weight(m1, prev[ck][1] if ck in prev else None, rule=a.start_rule)
                        start.append(s0); grow.append(m3 / s0)
                    else:
                        if m == 1: log[f'L{lvl}_qcew_weight_missing'] += 1
                        start.append(0.0); grow.append(L['EmpEnd'] / L['Emp'] if L['Emp'] > 0 else 1.0)
                    r = rates(lvl, k[1], par, y, q)
                    if r is None:
                        r = {'gain': L['FrmJbGn'] / max(L['Emp'], 1), 'churn': max(L['HirA'] - L['FrmJbGn'], 0) / max(L['Emp'], 1),
                             'stable': L['EmpS'] / max(L['Emp'], 1), 'earn': L['EarnS'], 'newshare': 0.7, 'endshare': 0.6}
                    rt.append(r)
                if sum(start) <= 0: start = [1.0] * len(unk)
                rng = cond.rng_for(a.seed, m, c, par, lvl, y, q)
                z = {name: [rng.gauss(0, 1) for _ in unk] for name in ('emp', 'net', 'gain', 'hire', 'stable', 'earn', 'hirn', 'hend')}
                Lp = dict(L)
                if Lp['Emp'] <= 0: Lp['Emp'] = 1e-6   # allocate() needs a positive start total
                out, _ = ds.allocate(Lp, start, grow, rt, z, {v: [1.0] * len(unk) for v in ('Emp', 'gain', 'churn')}, a)
                for k, d in zip(unk, out): val[lvl][k] = d
                if m == 1:
                    tot = sum(start)
                    for k, s_ in zip(unk, start):
                        baseline[k] = {v: (L[v] * s_ / tot if v != 'EarnS' else L['EarnS']) for v in L if L[v] is not None} | {'FrmJbC': (L['EmpEnd'] - L['Emp']) * s_ / tot}
                if m == 1: log[f'L{lvl}_groups_allocated'] += 1
            for k, d in val[lvl].items():
                if k not in known[lvl] or known[lvl][k] is None:
                    results[(lvl,) + k].append(d)
                    log['identity_violations'] += cond.identity_violations(d) if 'FrmJbC' in d else 0
                    for v in VARS:
                        if v != 'FrmJbC' and isinstance(d.get(v), float) and d[v] < -1e-6:
                            log['negative_values'] += 1; log[f'negative_{v}_L{lvl}_{"single" if len(groups[lvl][(k[0], qwi_parent(k[1], lvl), k[2], k[3])]) and sum(1 for kk in groups[lvl][(k[0], qwi_parent(k[1], lvl), k[2], k[3])] if kk not in known[lvl] or known[lvl][kk] is None) == 1 else "multi"}'] += 1

    os.makedirs(a.out, exist_ok=True)
    print('log:', dict(log))
    if not a.validate:
        with gzip.open(os.path.join(a.out, f'qwi_imputed_county_M{a.draws}.csv.gz'), 'wt', newline='') as fh:
            w = csv.writer(fh); w.writerow(['draw', 'area_fips', 'industry_code', 'ind_level', 'year', 'quarter'] + VARS)
            for (lvl, c, k, y, q), dd in sorted(results.items()):
                for m, d in enumerate(dd, 1):
                    row = {v: cond.out_value(d.get(v)) for v in VARS}; row['EarnS'] = cond.earnings_out(d.get('EarnS'), d.get('EmpS'))
                    w.writerow([m, c, k, lvl, y, q] + [row[v] for v in VARS])
        open(os.path.join(a.out, 'log_qwi_imputed_county.txt'), 'w').write(str(dict(log)) + '\n')
        return

    rows = []
    for k, truth in hidden.items():
        dd = results.get(('4',) + k)
        if not dd or len(dd) < a.draws or k not in baseline: continue
        for v in VARS:
            if truth.get(v) is None and v != 'FrmJbC': continue
            tv = truth['EmpEnd'] - truth['Emp'] if v == 'FrmJbC' else truth[v]
            xs = sorted(d[v] for d in dd if d.get(v) is not None)
            if len(xs) < a.draws or v not in baseline[k]: continue
            rows.append({'area_fips': k[0], 'industry_code': k[1], 'year': k[2], 'quarter': k[3], 'var': v, 'truth': tv, 'alloc_mean': st.mean(xs),
                         'alloc_p10': xs[int(.1 * len(xs))], 'alloc_p90': xs[min(len(xs) - 1, int(.9 * len(xs)))], 'baseline': baseline[k][v]})
    vdir = os.path.join(a.out, 'validation'); os.makedirs(vdir, exist_ok=True)
    with gzip.open(os.path.join(vdir, 'validation_hidden_n4.csv.gz'), 'wt', newline='') as fh:
        wr = csv.DictWriter(fh, list(rows[0])); wr.writeheader(); wr.writerows(rows)
    lines = [f'QWI imputation hide-and-check: county 4-digit, MD 2021-2024, private; hidden published cells with Emp <= {a.hide_max_emp:g} '
             f'and a suppressed sibling ({len(hidden)} cells), M={a.draws}',
             f'  {"variable":8s} {"n":>6s} | {"alloc med|err|":>14s} {"alloc wAPE":>10s} {"80% cover":>9s} | {"base med|err|":>13s} {"base wAPE":>9s} | {"median truth":>12s}']
    for v in VARS:
        r = [x for x in rows if x['var'] == v]
        if not r: continue
        ea = [abs(x['alloc_mean'] - x['truth']) for x in r]; eb = [abs(x['baseline'] - x['truth']) for x in r]
        tt = sum(abs(x['truth']) for x in r) or 1
        cov = st.mean(x['alloc_p10'] - 0.5 <= x['truth'] <= x['alloc_p90'] + 0.5 for x in r)
        lines.append(f'  {v:8s} {len(r):6d} | {st.median(ea):14.1f} {sum(ea) / tt:10.1%} {cov:9.2f} | {st.median(eb):13.1f} {sum(eb) / tt:9.1%} | {st.median(x["truth"] for x in r):12.0f}')
    lines += ['wAPE = sum |error| / sum |truth|; allocation error uses the mean of the draws.', 'log: ' + str(dict(log))]
    print('\n'.join(lines))
    open(os.path.join(vdir, 'summary_hidden_n4.txt'), 'w').write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
