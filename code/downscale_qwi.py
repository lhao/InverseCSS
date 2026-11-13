"""Downscale QWI county stocks and flows from a coarser industry level to a finer one (soft allocation), v2.

For each county x parent industry x quarter with published QWI totals, split the parent among its child
industries, M times. Every split uses weights w_k x exp(sigma_k * z - sigma_k^2 / 2), z ~ N(0,1).

  Emp      weights: QCEW month 3 of the previous quarter (closest to QWI's beginning of quarter), unless it
           differs from this quarter's month 1 by more than 1.5x (an industry reclassification break), then month 1;
           published or imputed draw m; sigma_k by QCEW size (SIGMA_STOCK, calibrated) x stock_scale
  net chg  child QCEW within-quarter growth (month3 / month1), mixed with the parent's proportional change
           (weight <= trust_growth), plus its own noise: sd = sigma_net x Emp_k x statewide gain rate_k;
           shrunk just enough that positive changes fit inside the parent's job gains and end employment >= 0
  EmpEnd   = Emp + net change;  FrmJbC = net change
  FrmJbGn  = max(FrmJbC, 0) + share of the rest, weights Emp_k x statewide gain rate (sigma_flow)
  FrmJbLs  = FrmJbGn - FrmJbC                                  (identity, >= 0)
  HirA     = FrmJbGn + share of (parent HirA - FrmJbGn), weights Emp_k x statewide churn rate (sigma_flow)
  Sep      = HirA - FrmJbC                                     (identity, >= FrmJbLs)
  HirN     = share of parent HirN, weights HirA_k x statewide new-hire share, 0 <= HirN <= HirA;  HirR = HirA - HirN
  HirAEnd  = share of parent HirAEnd, weights HirA_k x statewide end-share, FrmJbGn <= HirAEnd <= min(HirA, EmpEnd)
  SepBeg   = HirAEnd - FrmJbC                                  (identity; <= Sep and <= Emp by the bounds)
  EmpS     = parent EmpS split by Emp_k x statewide stable share, capped at min(Emp, EmpEnd)
  EarnS    = statewide EarnS of the child x noise (sigma_earn x (1 + c/sqrt(EmpS))) x a factor reproducing the parent's stable payroll
HirN/HirR and HirAEnd/SepBeg are filled only where the parent publishes them.

Cross-county adjustment (--passes > 1): after each pass, for every child industry x quarter, compare the
sum over counties of Emp, FrmJbGn and HirA with the QWI state value of the child industry (restricted to the
counties allocated, using QCEW county shares), and multiply that industry's weights by (target/sum)^omega,
omega = var_sum / (var_sum + var_state). Allocation is re-run with the same random numbers, so parent totals,
identities and bounds still hold exactly.

Baseline: every variable split in proportion to QCEW month-1 employment (EarnS = parent EarnS).

Known data conditions are handled by the shared module conditions.py (docs/DATA_CONDITIONS.md).

Usage:
  python3 downscale_qwi.py <parent_qwi.csv.gz> <child_qwi.csv.gz> <child_state_qwi.csv.gz>
         <qcew_raw_root> <qcew_imputed_dir> <child_level> <out_dir>
         [--draws M] [--sigma-flow S] [--sigma-earn S] [--sigma-net S] [--stock-scale S] [--trust-growth T]
         [--passes P] [--only c:ind,...] [--seed S] [--validate]
"""
import argparse, collections, csv, gzip, math, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond   # shared handling of known data conditions (docs/DATA_CONDITIONS.md)

VARS = ['Emp', 'EmpEnd', 'EmpS', 'HirA', 'HirN', 'HirR', 'Sep', 'HirAEnd', 'SepBeg', 'FrmJbGn', 'FrmJbLs', 'FrmJbC', 'EarnS']
NEED = ['Emp', 'EmpEnd', 'EmpS', 'HirA', 'Sep', 'FrmJbGn', 'FrmJbLs', 'EarnS']
OPTIONAL = ['HirN', 'HirR', 'HirAEnd', 'SepBeg']
SLICES = {('24047', '721'), ('24047', '722'), ('24031', '541'), ('24047', '7211'), ('24047', '7225'), ('24031', '5417')}
SIGMA_STOCK, SIGMA_STATE, sig, num = cond.SIGMA_STOCK, cond.SIGMA_STATE, cond.sigma, cond.qwi_value   # B5, A5
STATE = '24000'


def load_qcew(raw_root, imp_dir, lvl, draws, complete_dir=''):
    """{(y, q): {draw: {(county, ind): (m1, m3)}}} for private (own 5) county cells at the child level.
    Published (and true-zero) cells from the raw files; suppressed cells from imputed draw m (A1, A2, F1, F5).
    If complete_dir holds qcew_complete_<y>q<q>_M20.csv.gz for a quarter (NAICS-2022 conversion of a NAICS-2017
    quarter, C1), that file is used instead of raw + imputed for the quarter."""
    agg = cond.county_agglvl(lvl)
    out = {}
    for y, q, qdir in cond.quarter_dirs(raw_root):
        cp = os.path.join(complete_dir, f'qcew_complete_{y}q{q}_M20.csv.gz') if complete_dir else ''
        if cp and os.path.exists(cp):
            per = {m: {} for m in range(1, draws + 1)}
            with cond.open_text(cp) as fh:
                for r in csv.DictReader(fh):
                    m = int(r['draw'])
                    if m <= draws and r['own_code'] == '5' and r['level'] == lvl and r['area_fips'] != '24000':
                        per[m][(r['area_fips'], r['industry_code'])] = (float(r['month1_emplvl']), float(r['month3_emplvl']))
            out[(y, q)] = per
            continue
        pub = {}
        for f in cond.area_files(qdir):
            with cond.open_text(f) as fh:
                for r in csv.DictReader(fh):
                    if r['agglvl_code'] == agg and r['own_code'] == '5' and cond.qcew_status(r) in ('published', 'true_zero'):
                        pub[(r['area_fips'], r['industry_code'])] = (cond.qcew_value(r, 'month1_emplvl'), cond.qcew_value(r, 'month3_emplvl'))
        per = {m: dict(pub) for m in range(1, draws + 1)}
        with cond.open_text(os.path.join(imp_dir, f'qcew_imputed_{y}q{q}_M20.csv.gz')) as fh:
            for r in csv.DictReader(fh):
                m = int(r['draw'])
                if m <= draws and r['own_code'] == '5' and r['level'] == lvl:
                    per[m][(r['area_fips'], r['industry_code'])] = (float(r['month1_emplvl']), float(r['month3_emplvl']))
        out[(y, q)] = per
    return out


def shares(w, z, sigmas):
    """Weights x multiplicative lognormal noise (mean 1) -> shares. z: pre-drawn N(0,1)."""
    x = [wi * math.exp(s * zi - s * s / 2) for wi, zi, s in zip(w, z, sigmas)]
    t = sum(x)
    return [xi / t for xi in x] if t > 0 else [1 / len(w)] * len(w)


def bounded_split(total, lb, ub, sh):
    """total = sum(lb) + free split by shares, respecting lb <= x <= ub (water-filling)."""
    x = list(lb)
    free = total - sum(lb)
    if free <= 0:
        return [total * l / sum(lb) for l in lb] if sum(lb) > 0 else [total / len(lb)] * len(lb)
    active = [i for i in range(len(x)) if ub[i] > lb[i]]
    for _ in range(len(x) + 1):
        if not active or free <= 1e-9: break
        s = sum(sh[i] for i in active) or len(active)
        over, nxt = 0.0, []
        for i in active:
            add = free * (sh[i] / s if s else 1 / len(active))
            if x[i] + add > ub[i]: over += x[i] + add - ub[i]; x[i] = ub[i]
            else: x[i] += add; nxt.append(i)
        free, active = over, nxt
    return x


def allocate(pv, start, grow, rt, z, mult, a):
    """One draw for one parent. z: dict of pre-drawn normals; mult: weight multipliers per child for Emp/gain/churn."""
    n = len(start)
    E = max(pv['Emp'], 1e-9)          # guard: an imputed parent may have zero start employment in some draws
    emp = [pv['Emp'] * x for x in shares([s * m for s, m in zip(start, mult['Emp'])], z['emp'], [a.stock_scale * sig(SIGMA_STOCK, s) for s in start])]
    C = pv['EmpEnd'] - pv['Emp']
    prop = [C * e / E for e in emp]
    raw = [e * (g - 1) for e, g in zip(emp, grow)]
    raw = [x - (sum(raw) - C) * e / E for x, e in zip(raw, emp)]
    noise = [a.sigma_net * e * r['gain'] * zz for e, r, zz in zip(emp, rt, z['net'])]
    noise = [x - sum(noise) * e / E for x, e in zip(noise, emp)]          # keeps the parent total
    def chg_at(l):
        return [l * (a.trust_growth * x + (1 - a.trust_growth) * p_ + nz) + (1 - l) * p_ for x, p_, nz in zip(raw, prop, noise)]
    def ok(c_):
        return sum(max(x, 0) for x in c_) <= pv['FrmJbGn'] + 1e-9 and all(e + x >= -1e-9 for e, x in zip(emp, c_))
    lam = 1.0
    if not ok(chg_at(1.0)):
        lo, hi = 0.0, 1.0
        for _ in range(30):
            mid = (lo + hi) / 2
            if ok(chg_at(mid)): lo = mid
            else: hi = mid
        lam = lo
    chg = chg_at(lam)
    end = [max(e + x, 0.0) for e, x in zip(emp, chg)]
    chg = [e1 - e0 for e0, e1 in zip(emp, end)]
    gsh = shares([e * r['gain'] * m for e, r, m in zip(emp, rt, mult['gain'])], z['gain'], [a.sigma_flow] * n)
    gn = bounded_split(pv['FrmJbGn'], [max(x, 0.0) for x in chg], [float('inf')] * n, gsh)
    ls = [g - x for g, x in zip(gn, chg)]
    hsh = shares([e * max(r['churn'], 1e-6) * m for e, r, m in zip(emp, rt, mult['churn'])], z['hire'], [a.sigma_flow] * n)
    hi_ = bounded_split(pv['HirA'], gn, [float('inf')] * n, hsh)
    sp = [h - x for h, x in zip(hi_, chg)]
    cap = [min(e0, e1) for e0, e1 in zip(emp, end)]
    es = bounded_split(pv['EmpS'], [0.0] * n, cap, shares([e * r['stable'] for e, r in zip(emp, rt)], z['stable'], [0.05] * n))
    # earnings noise grows for small cells: sigma_earn x (1 + c / sqrt(stable jobs))  (coverage was 0.67 for small cells)
    # capped at 0.8 and mean-corrected (exp(s z - s^2/2)), so tiny cells cannot produce extreme draws
    se = [min(a.sigma_earn * (1 + getattr(a, 'earn_size_c', 0.0) / math.sqrt(max(x, 1.0))), 0.8) for x in es]
    ew = [r['earn'] * math.exp(sg * zz - sg * sg / 2) for r, zz, sg in zip(rt, z['earn'], se)]
    den = sum(x * w for x, w in zip(es, ew))
    earn = [w * pv['EarnS'] * pv['EmpS'] / den if den > 0 else pv['EarnS'] for w in ew]
    out = [{'Emp': emp[i], 'EmpEnd': end[i], 'FrmJbC': chg[i], 'FrmJbGn': gn[i], 'FrmJbLs': ls[i], 'HirA': hi_[i], 'Sep': sp[i],
            'EmpS': es[i], 'EarnS': earn[i]} for i in range(n)]
    if pv.get('HirN') is not None and pv.get('HirR') is not None:
        hn = bounded_split(pv['HirN'], [0.0] * n, hi_, shares([h * r['newshare'] for h, r in zip(hi_, rt)], z['hirn'], [a.sigma_flow] * n))
        for i in range(n): out[i]['HirN'] = hn[i]; out[i]['HirR'] = hi_[i] - hn[i]
    if pv.get('HirAEnd') is not None and pv.get('SepBeg') is not None:
        he = bounded_split(pv['HirAEnd'], list(gn), [max(min(h, e1), g) for h, e1, g in zip(hi_, end, gn)],   # B10: HirAEnd >= FrmJbGn
                           shares([h * r['endshare'] for h, r in zip(hi_, rt)], z['hend'], [a.sigma_flow] * n))
        for i in range(n): out[i]['HirAEnd'] = he[i]; out[i]['SepBeg'] = he[i] - chg[i]
    return out, lam < 1.0


def main():
    ap = argparse.ArgumentParser()
    for x in ('parent', 'child', 'child_state', 'qcew_raw', 'qcew_imp', 'child_level', 'out'): ap.add_argument(x)
    ap.add_argument('--draws', type=int, default=20)
    ap.add_argument('--sigma-flow', type=float, default=0.75); ap.add_argument('--sigma-earn', type=float, default=0.3)
    ap.add_argument('--earn-size-c', type=float, default=0.0, help='earnings noise multiplier 1 + c/sqrt(EmpS) for small cells')
    ap.add_argument('--start-rule', default='hybrid', choices=cond.START_RULES, help='start-of-quarter weight rule (conditions.start_weight)')
    ap.add_argument('--sigma-net', type=float, default=1.0, help='net-change noise, in units of the child gain rate x Emp')
    ap.add_argument('--stock-scale', type=float, default=1.5); ap.add_argument('--trust-growth', type=float, default=0.3)
    ap.add_argument('--passes', type=int, default=1, help='>1 adds the cross-county adjustment toward QWI state child totals')
    ap.add_argument('--only', default='', help='comma list county:parent_industry, e.g. 24047:7211,24031:5417')
    ap.add_argument('--seed', type=int, default=1); ap.add_argument('--validate', action='store_true')
    ap.add_argument('--parent-imputed', default='', help='impute_qwi.py output: suppressed parents take imputed version m in draw m')
    ap.add_argument('--qcew-complete', default='', help='folder with NAICS-2022 converted QCEW quarters (convert_qcew_naics.py)')
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])  # F11
    plvl = str(int(a.child_level) - 1) if a.child_level != '6' else '4'
    parents = cond.load_qwi(a.parent, 'C', plvl)
    children = cond.load_qwi(a.child, 'C', a.child_level)
    st_child = cond.load_qwi(a.child_state, 'S', a.child_level)
    st_parent = cond.load_qwi(a.parent, 'S', plvl)
    qcew = load_qcew(a.qcew_raw, a.qcew_imp, a.child_level, a.draws, a.qcew_complete)
    log = collections.Counter()
    imputed = collections.defaultdict(dict)      # (c, ind, y, q) -> {draw: values}
    if a.parent_imputed:
        with cond.open_text(a.parent_imputed) as fh:
            for r in csv.DictReader(fh):
                if r['ind_level'] != plvl or int(r['draw']) > a.draws: continue
                d = {v: (float(r[v]) if r[v] != '' else None) for v in NEED + OPTIONAL}
                if d['EarnS'] is None: d['EarnS'] = 0.0
                imputed[(r['area_fips'], r['industry_code'], int(r['year']), int(r['quarter']))][int(r['draw'])] = d

    def rates(ind, par, y, q):
        for src, key in ((st_child, (STATE, ind, y, q)), (st_parent, (STATE, par, y, q))):
            r = src.get(key)
            if r and num(r, 'Emp'):
                e = num(r, 'Emp'); v = {x: num(r, x) for x in ('FrmJbGn', 'HirA', 'EmpS', 'EarnS', 'HirN', 'HirAEnd')}
                if None not in (v['FrmJbGn'], v['HirA'], v['EmpS'], v['EarnS']):
                    return {'gain': v['FrmJbGn'] / e, 'churn': max(v['HirA'] - v['FrmJbGn'], 0) / e, 'stable': v['EmpS'] / e, 'earn': v['EarnS'],
                            'newshare': (v['HirN'] / v['HirA']) if v['HirN'] is not None and v['HirA'] else 0.7,
                            'endshare': (v['HirAEnd'] / v['HirA']) if v['HirAEnd'] is not None and v['HirA'] else 0.6}
        return None

    kids = collections.defaultdict(set)
    for (y, q), per in qcew.items():
        for (c, ind) in per[1]: kids[(c, ind[:int(plvl)])].add(ind)
    for (c, ind, y, q) in children: kids[(c, ind[:int(plvl)])].add(ind)

    only = {tuple(x.split(':')) for x in a.only.split(',') if x}
    jobs = []      # parents to allocate, with fixed inputs
    single = []    # parents with one child: passed through unchanged
    for (c, par, y, q), P in sorted(parents.items()):
        if only and (c, par) not in only: continue
        pv = {v: num(P, v) for v in NEED}
        if None in pv.values():
            imp = imputed.get((c, par, y, q))
            if not imp or len(imp) < a.draws: log['parent_not_fully_published'] += 1; continue
            pvs = [dict(imp[m]) for m in range(1, a.draws + 1)]   # suppressed parent: imputed version m in draw m
            log['parent_from_imputation'] += 1
        else:
            for v in OPTIONAL: pv[v] = num(P, v)
            pvs = [pv] * a.draws
        for x in {id(x): x for x in pvs}.values():
            adj = cond.make_consistent(x)       # B1: identities exact (published values hold only within +-2)
            x.pop('FrmJbC', None)
            if adj: log['parent_made_consistent'] += 1; log['parent_adjust_max'] = max(log['parent_adjust_max'], adj)
        pv = pvs[0]
        ks = sorted(kids.get((c, par), []))
        if len(ks) == 1:                       # the only child equals the parent
            log['parent_single_child_passed_through'] += 1
            ds_ = [{v: x[v] for v in NEED + OPTIONAL if x.get(v) is not None} | {'FrmJbC': x['EmpEnd'] - x['Emp']} for x in pvs]
            single.append(((c, ks[0], y, q), ds_)); continue
        if not ks: log['parent_without_children'] += 1; continue
        if pv['Emp'] <= 0 and all(x['Emp'] <= 0 for x in pvs):
            log['parent_zero_employment'] += 1
            for k in ks: single.append(((c, k, y, q), [{v: 0.0 for v in VARS if v != 'EarnS'} | {'EarnS': x['EarnS']} for x in pvs]))
            continue
        rt = []
        for k in ks:
            r = rates(k, par, y, q)
            if r is None:
                log['rates_from_parent_county'] += 1
                E = max(pv['Emp'], 1.0)
                r = {'gain': pv['FrmJbGn'] / E, 'churn': max(pv['HirA'] - pv['FrmJbGn'], 0) / E, 'stable': pv['EmpS'] / E,
                     'earn': pv['EarnS'], 'newshare': 0.7, 'endshare': 0.6}
            rt.append(r)
        jobs.append((c, par, y, q, pvs, ks, rt))
    log['parents_allocated'] = len(jobs)

    results = collections.defaultdict(list)
    baseline = {}
    for m in range(1, a.draws + 1):
        inputs = []
        for (c, par, y, q, pvs, ks, rt) in jobs:
            pv = pvs[m - 1]
            cur = qcew.get((y, q), {}).get(m, {})
            py, pq = (y, q - 1) if q > 1 else (y - 1, 4)
            prev = qcew.get((py, pq), {}).get(m, {})
            start, grow = [], []
            for k in ks:
                if (c, k) in cur and cur[(c, k)][0] > 0:
                    m1, m3 = cur[(c, k)]
                    s0, brk = cond.start_weight(m1, prev[(c, k)][1] if (c, k) in prev else None, rule=a.start_rule)   # E1, C2
                    if brk and m == 1: log['reclassification_break'] += 1
                    start.append(s0); grow.append(m3 / s0)
                else:
                    if m == 1: log['qcew_weight_missing'] += 1
                    start.append(0.0); grow.append(pv['EmpEnd'] / pv['Emp'] if pv['Emp'] > 0 else 1.0)
            rng = cond.rng_for(a.seed, m, c, par, y, q)   # F4
            z = {name: [rng.gauss(0, 1) for _ in ks] for name in ('emp', 'net', 'gain', 'hire', 'stable', 'earn', 'hirn', 'hend')}
            inputs.append((start, grow, z))
            if m == 1:
                p = [s / sum(start) for s in start] if sum(start) > 0 else [1 / len(ks)] * len(ks)
                for k, pk in zip(ks, p):
                    baseline[(c, k, y, q)] = {v: (pv[v] * pk if v != 'EarnS' else pv['EarnS']) for v in NEED + OPTIONAL if pv.get(v) is not None} \
                                             | {'FrmJbC': (pv['EmpEnd'] - pv['Emp']) * pk}
        mult = {(j[0], j[1], j[2], j[3]): {v: [1.0] * len(j[5]) for v in ('Emp', 'gain', 'churn')} for j in jobs}
        for ps in range(a.passes):
            draw = {}
            for job, (start, grow, z) in zip(jobs, inputs):
                c, par, y, q, pvs, ks, rt = job; pv = pvs[m - 1]
                out, shrunk = allocate(pv, start, grow, rt, z, mult[(c, par, y, q)], a)
                if ps == a.passes - 1: log['net_change_shrunk'] += shrunk
                draw[(c, par, y, q)] = out
            if ps == a.passes - 1: break
            # cross-county adjustment toward QWI state child totals
            sums = collections.defaultdict(lambda: collections.defaultdict(float)); var = collections.defaultdict(lambda: collections.defaultdict(float))
            alloc_q, all_q = collections.defaultdict(float), collections.defaultdict(float)
            for job, (start, grow, z) in zip(jobs, inputs):
                c, par, y, q, pvs, ks, rt = job; pv = pvs[m - 1]
                for k, s, d in zip(ks, start, draw[(c, par, y, q)]):
                    alloc_q[(k, y, q)] += s
                    for v, sg in (('Emp', a.stock_scale * sig(SIGMA_STOCK, s)), ('FrmJbGn', a.sigma_flow), ('HirA', a.sigma_flow)):
                        sums[(k, y, q)][v] += d[v]; var[(k, y, q)][v] += (sg * d[v]) ** 2
            for (c, k, y, q), ds_ in single:        # fixed contributions of pass-through cells
                d = ds_[m - 1]
                alloc_q[(k, y, q)] += qcew.get((y, q), {}).get(m, {}).get((c, k), (0.0, 0.0))[0]
                for v in ('Emp', 'FrmJbGn', 'HirA'): sums[(k, y, q)][v] += d[v]
            for (y, q), per in qcew.items():
                for (c, ind), (m1, m3) in per[m].items(): all_q[(ind, y, q)] += m1
            fac = {}
            for (k, y, q), s in sums.items():
                t = st_child.get((STATE, k, y, q))
                if not t or all_q[(k, y, q)] <= 0: continue
                cover = alloc_q[(k, y, q)] / all_q[(k, y, q)]
                f = {}
                for v in ('Emp', 'FrmJbGn', 'HirA'):
                    tv = num(t, v)
                    if tv is None or s[v] <= 0: continue
                    target = tv * cover
                    vs = var[(k, y, q)][v] / s[v] ** 2; vt = sig(SIGMA_STATE, tv) ** 2
                    f[v] = min(max((target / s[v]) ** (vs / (vs + vt)), 0.5), 2.0)
                fac[(k, y, q)] = f
            for job in jobs:
                c, par, y, q, pvs, ks, rt = job; pv = pvs[m - 1]
                for i, k in enumerate(ks):
                    f = fac.get((k, y, q), {})
                    mult[(c, par, y, q)]['Emp'][i] *= f.get('Emp', 1.0)
                    mult[(c, par, y, q)]['gain'][i] *= f.get('FrmJbGn', 1.0)
                    mult[(c, par, y, q)]['churn'][i] *= f.get('HirA', 1.0)
        for job in jobs:
            c, par, y, q, pvs, ks, rt = job; pv = pvs[m - 1]
            out = draw[(c, par, y, q)]
            for k, d in zip(ks, out): results[(c, k, y, q)].append(d)
            log['max_parent_gap_Sep'] = max(log['max_parent_gap_Sep'], round(abs(sum(d['Sep'] for d in out) - pv['Sep'])))
            log['max_parent_gap_FrmJbLs'] = max(log['max_parent_gap_FrmJbLs'], round(abs(sum(d['FrmJbLs'] for d in out) - pv['FrmJbLs'])))
            for d in out:
                for v in VARS:
                    if v != 'FrmJbC' and v in d and d[v] < -1e-6: log['negative_values'] += 1; log['negative_' + v] += 1
            log['identity_violations'] += sum(cond.identity_violations(d) for d in out)   # R4

    for key, ds_ in single:
        results[key] = [dict(d) for d in ds_]
        if key not in baseline: baseline[key] = dict(ds_[0])
    os.makedirs(a.out, exist_ok=True)
    tag = f'sn{a.sigma_net:g}_p{a.passes}'
    print('log:', dict(log))
    if not a.validate:
        with gzip.open(os.path.join(a.out, f'qwi_downscaled_n{a.child_level}_M{a.draws}_{tag}.csv.gz'), 'wt', newline='') as fh:
            w = csv.writer(fh); w.writerow(['draw', 'area_fips', 'industry_code', 'year', 'quarter'] + VARS)
            for (c, k, y, q), ds in sorted(results.items()):
                for m, d in enumerate(ds, 1):
                    row = {v: cond.out_value(d.get(v)) for v in VARS}                       # F6
                    row['EarnS'] = cond.earnings_out(d.get('EarnS'), d.get('EmpS'))
                    w.writerow([m, c, k, y, q] + [row[v] for v in VARS])
        open(os.path.join(a.out, f'log_n{a.child_level}_{tag}.txt'), 'w').write(str(dict(log)) + '\n')
        return

    rows = []
    for key, ds in results.items():
        t = children.get(key)
        if not t: continue
        for v in VARS:
            tv = num(t, v)
            xs = sorted(d[v] for d in ds if v in d)
            if tv is None or len(xs) < len(ds) or v not in baseline[key]: continue
            rows.append({'area_fips': key[0], 'industry_code': key[1], 'year': key[2], 'quarter': key[3], 'var': v, 'truth': tv,
                         'alloc_mean': st.mean(xs), 'alloc_p10': xs[int(.1 * len(xs))], 'alloc_p90': xs[min(len(xs) - 1, int(.9 * len(xs)))],
                         'baseline': baseline[key][v], 'slice': (key[0], key[1]) in SLICES})
    with gzip.open(os.path.join(a.out, f'validation_n{plvl}_to_n{a.child_level}_{tag}.csv.gz'), 'wt', newline='') as fh:
        w = csv.DictWriter(fh, list(rows[0])); w.writeheader(); w.writerows(rows)

    def report(sel, title):
        lines = [title, f'  {"variable":8s} {"n":>6s} | {"alloc med|err|":>14s} {"alloc wAPE":>10s} {"80% cover":>9s} | {"base med|err|":>13s} {"base wAPE":>9s} | {"median truth":>12s}']
        for v in VARS:
            r = [x for x in sel if x['var'] == v]
            if not r: continue
            ea = [abs(x['alloc_mean'] - x['truth']) for x in r]; eb = [abs(x['baseline'] - x['truth']) for x in r]
            tt = sum(abs(x['truth']) for x in r) or 1
            cov = st.mean(x['alloc_p10'] - 0.5 <= x['truth'] <= x['alloc_p90'] + 0.5 for x in r)
            lines.append(f'  {v:8s} {len(r):6d} | {st.median(ea):14.1f} {sum(ea) / tt:10.1%} {cov:9.2f} | {st.median(eb):13.1f} {sum(eb) / tt:9.1%} | {st.median(x["truth"] for x in r):12.0f}')
        return lines
    out = report(rows, f'Downscaling validation v2: county {plvl}-digit -> {a.child_level}-digit, MD 2021-2024, private, sigma_net={a.sigma_net:g}, passes={a.passes}, '
                       f'sigma_flow={a.sigma_flow:g}, sigma_earn={a.sigma_earn:g}, stock_scale={a.stock_scale:g}, trust_growth={a.trust_growth:g}, M={a.draws}')
    out += [''] + report([x for x in rows if x['slice']], 'Pilot slices only (Worcester 721->7211, 722->7225; Montgomery 541->5417)')
    out += ['', 'wAPE = sum |error| / sum |truth|; allocation error uses the mean of the draws; EarnS is an average ($/month).', 'log: ' + str(dict(log))]
    print('\n'.join(out))
    open(os.path.join(a.out, f'summary_n{plvl}_to_n{a.child_level}_{tag}.txt'), 'w').write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
