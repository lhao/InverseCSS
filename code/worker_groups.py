"""Step 4 (R2): worker groups per synthetic establishment, pilot slices (stocks; flows by group come next).

Worker type t = sex (2) x age-education (23: ages A01-A03 with E5 'not available, under 25'; ages A04-A08 x E1-E4)
x race-ethnicity (5: Hispanic of any race; non-Hispanic White, Black, Asian, other) = 230 types. Employment =
the establishment's average monthly employment (panel emp_avg).

1. Cell x firm-size composition (same in both versions; constraints from published tables). Each slice is a whole
   county 4-digit industry, so the QWI county 4-digit tables (sex x age, sex x education, race x ethnicity) are its
   totals. Suppressed groups share the leftover (total - published) by state shares. For each table: seed =
   employment of (6-digit cell, firm-size class) x county share x tilt, tilt = state 6-digit x firm-size share / state
   4-digit share (published; capped 0.1-10); raked (IPF) so the slice reproduces the county composition and every
   (cell, size) keeps its employment. Sex is shared by two tables: sex x education is rescaled to the sex totals of
   sex x age.
2. Joint of the three tables within each (cell, size), never published: IPF over the 230 types to the three margins.
     random      uniform start over feasible types (independence given the margins)
     systematic  ACS workplace joint (1-year 2021-2024, the cell's sector x place-of-work area; fallback sector
                 statewide), with a small floor, as the start
3. Establishments within (cell, size):
     random      proportional to employment (the maximum-entropy table: no segregation across establishments)
     systematic  persistent establishment tilts per dimension (sex, age, race-ethnicity, education), drawn once per
                 establishment from a Dirichlet whose concentration matches LODES between-block dispersion in the
                 sector (beta-binomial ICC, blocks with >= 80% of jobs in the sector); splits inherit the parent's tilts
   then raked so each establishment keeps its employment and the (cell, size) types their totals (exact).

Outputs (<out_dir>): workers_<tag>.csv.gz (version, year, quarter, county, naics6, est_id, firm_size_class, type,
emp; entries >= 0.005), worker_report_<tag>.txt (constraint checks, held-out check vs QWI county x sector x firm size x
sex x age, segregation)
Usage: python3 worker_groups.py <panel_dir> <qwi_dir> <acs_workplace.parquet> <lodes_wac.csv.gz> <out_dir>
                                [--prior random|systematic] [--draws 2] [--seed 21] [--tag slices]
"""
import argparse, collections, csv, gzip, math, os, random, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

SEX = ['1', '2']
AGES = ['A01', 'A02', 'A03', 'A04', 'A05', 'A06', 'A07', 'A08']
YOUNG = {'A01', 'A02', 'A03'}
EDU = ['E1', 'E2', 'E3', 'E4', 'E5']
RE = ['H', 'W', 'B', 'A', 'O']
SIZES = priors.SIZES
TYPES = [(s, a, e, r) for s in SEX for a in AGES for e in EDU for r in RE if (e == 'E5') == (a in YOUNG)]
LODES_DIMS = {'sex': (['CS01', 'CS02'], lambda t: SEX.index(t[0])),
              'age': (['CA01', 'CA02', 'CA03'], lambda t: 0 if t[1] in YOUNG or t[1] == 'A04' else (1 if t[1] in ('A05', 'A06') else 2)),
              'race': (['CR01', 'CR02', 'CR04', 'CR03'], lambda t: {'W': 0, 'B': 1, 'A': 2}.get(t[3], 3)),
              'edu': (['CD01', 'CD02', 'CD03', 'CD04'], lambda t: EDU.index(t[2]) if t[2] != 'E5' else None)}


def re_group(race, eth):
    if eth == 'A2': return 'H'
    return {'A1': 'W', 'A2': 'B', 'A4': 'A'}.get(race, 'O')


MEASURES = ('emp', 'HirA', 'Sep')


def load_qwi_demo(path, keep, geo):
    """{measure: {(area, industry, y, q, firmsize): {table_group: value}}} for one QWI demographic file; measures:
    emp = (Emp + EmpEnd) / 2, HirA (all hires), Sep (separations). Groups: ('sa', sex, age), ('se', sex, edu),
    ('rh', re-group); ('tot',) = all workers; ('supp', ...) marks a suppressed group."""
    out = {ms: collections.defaultdict(lambda: collections.defaultdict(float)) for ms in MEASURES}; supp = collections.Counter()
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != geo or r['ownercode'] != 'A05' or not keep(r['geography'], r['industry']): continue
            k = (r['geography'] if geo == 'C' else '24000', r['industry'], int(r['year']), int(r['quarter']), r['firmsize'])
            g = None
            if r['sex'] == '0' and r['agegrp'] == 'A00' and r['education'] == 'E0' and r['race'] == 'A0' and r['ethnicity'] == 'A0': g = ('tot',)
            elif r['sex'] in SEX and r['agegrp'] in AGES and r['education'] == 'E0': g = ('sa', r['sex'], r['agegrp'])
            elif r['sex'] in SEX and r['education'] in EDU and r['agegrp'] == 'A00': g = ('se', r['sex'], r['education'])
            elif r['race'] != 'A0' and r['ethnicity'] != 'A0' and r['sex'] == '0': g = ('rh', re_group(r['race'], r['ethnicity']))
            if g is None: continue
            vals = {'emp': None, 'HirA': cond.qwi_value(r, 'HirA'), 'Sep': cond.qwi_value(r, 'Sep')}
            v0, v1 = cond.qwi_value(r, 'Emp'), cond.qwi_value(r, 'EmpEnd')
            if v0 is not None and v1 is not None: vals['emp'] = (v0 + v1) / 2
            for ms, v in vals.items():
                if v is None: supp[f'{g[0]}_{ms}'] += 1; out[ms][k][('supp',) + g] = 1.0
                else: out[ms][k][g] += v
    return out, supp


def table_groups(name):
    if name == 'sa': return [('sa', s, a) for s in SEX for a in AGES]
    if name == 'se': return [('se', s, e) for s in SEX for e in EDU]
    return [('rh', r) for r in RE]


def shares(d, name, fallback=None):
    """Shares of a table's groups; suppressed groups share the leftover (total - published) by fallback shares."""
    G = table_groups(name); pub = {g: d[g] for g in G if g in d}; sup = [g for g in G if ('supp',) + g in d and g not in pub]
    tot = d.get(('tot',)); s_pub = sum(pub.values())
    if sup and tot and tot > s_pub and fallback:
        left = tot - s_pub; fs = sum(fallback.get(g, 0) for g in sup)
        for g in sup: pub[g] = left * (fallback.get(g, 0) / fs if fs > 0 else 1 / len(sup))
    s = sum(pub.values())
    return {g: pub.get(g, 0.0) / s for g in G} if s > 0 else None


def ipf_types(seed, margins, iters=80):
    """IPF of {type: value} to margins: list of (key function, {key: target}). numpy version (2026-10-24, F13 speed-up):
    same iterations and rules as before (a type whose margin group is empty is set to 0)."""
    import numpy as np
    ts = list(seed); m = np.array([seed[t] for t in ts], float)
    groups = []
    for kf, tgt in margins:
        ks = [kf(t) for t in ts]; uk = sorted(set(ks), key=str); idx = {k: i for i, k in enumerate(uk)}
        g = np.fromiter((idx[k] for k in ks), int, len(ks)); groups.append((g, np.array([tgt.get(k, 0.0) for k in uk], float)))
    for _ in range(iters):
        for g, tv in groups:
            cur = np.bincount(g, weights=m, minlength=len(tv))
            c = cur[g]; m = np.where(c > 0, m * tv[g] / np.where(c > 0, c, 1.0), 0.0)
    return dict(zip(ts, m.tolist()))


def lodes_icc(path, sectors):
    """{sector: {dimension: rho}}: beta-binomial ICC of demographic shares across LODES blocks dominated by the
    sector, measured WITHIN counties (each block's share compared with its county's mean), because the panel already
    carries between-county differences through each county's own composition. Blocks pool several establishments,
    so this understates establishment-level dispersion; LEHD education is imputed, so its rho is a lower bound."""
    col = {'71': 'CNS17', '72': 'CNS18', '54': 'CNS12', '62': 'CNS16', '44-45': 'CNS07'}
    B = collections.defaultdict(lambda: collections.defaultdict(list))
    with cond.open_text(cond.require(path)) as fh:
        for x in csv.DictReader(fh):
            tot = int(x['C000'])
            if tot < 10: continue
            for sec in sectors:
                c = col.get(sec)
                if c and int(x[c]) >= 0.8 * tot: B[sec][x['w_geocode'][:5]].append(x)
    out = {}
    for sec, byc in B.items():
        out[sec] = {}
        for d, (cols, _) in LODES_DIMS.items():
            num = den = wsum = 0.0
            for j, cname in enumerate(cols):
                nj = dj = pj = 0.0
                for cty, bl in byc.items():
                    xs = [(int(b[cname]), sum(int(b[c]) for c in cols)) for b in bl]
                    xs = [(a, n) for a, n in xs if n >= 5]
                    N = sum(n for _, n in xs)
                    if len(xs) < 3 or not N: continue
                    p = sum(a for a, _ in xs) / N
                    if p <= 0 or p >= 1: continue
                    nj += sum((a - n * p) ** 2 for a, n in xs) - sum(n * p * (1 - p) for _, n in xs)
                    dj += sum(n * (n - 1) * p * (1 - p) for _, n in xs); pj += p * N
                if dj > 0: num += pj * nj / dj; wsum += pj
            out[sec][d] = min(max(num / wsum if wsum else 0.0, 1e-4), 0.9)
    return out


def dirichlet(rng, alpha):
    g = [rng.gammavariate(max(x, 1e-6), 1.0) for x in alpha]; s = sum(g)
    return [x / s for x in g] if s > 0 else [1 / len(alpha)] * len(alpha)


def main():
    ap = argparse.ArgumentParser()
    for x in ('panel', 'qwi', 'acs', 'lodes', 'out'): ap.add_argument(x)
    ap.add_argument('--prior', default='random', choices=('random', 'systematic'))
    ap.add_argument('--draws', type=int, default=2); ap.add_argument('--seed', type=int, default=21); ap.add_argument('--tag', default='slices')
    ap.add_argument('--tilt-lambda', default='data/processed/demo_tilt_calibration/demo_tilt_lambda.csv',
                    help="D13: shrinkage of state firm-size tilts (calibrate_demo_tilts.py); '' = no shrinkage")
    a = ap.parse_args()
    cond.start_run(a.panel, a.qwi, a.acs, a.lodes)
    # panel establishments (stocks and flows) and exits (separations of closing establishments)
    E = collections.defaultdict(list); parents = {}; X_exits = collections.defaultdict(list)
    with cond.open_text(cond.require(os.path.join(a.panel, f'panel_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            m = int(r['version'])
            if m > a.draws: continue
            E[(m, int(r['year']), int(r['quarter']), r['county'], r['naics6'][:4])].append(
                (r['naics6'], r['firm_size_class'], r['est_id'], float(r['emp_avg']), float(r['hires'] or 0), float(r['separations'] or 0)))
            if r['event'] == 'split_successor' and r['pred_id']: parents[(m, r['est_id'])] = r['pred_id']
    with cond.open_text(cond.require(os.path.join(a.panel, f'exits_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            m = int(r['version'])
            if m <= a.draws and r['event'] == 'exit' and float(r['emp_prev_m3']) > 0:
                X_exits[(m, int(r['year']), int(r['quarter']), r['county'], r['naics6'][:4])].append((r['naics6'], r['firm_size_class'], r['est_id'], float(r['emp_prev_m3'])))
    keys4 = {(k[3], k[4]) for k in E}
    a.versions_seen = sorted({k[0] for k in E}) or [1]
    inds6 = {e[0] for v in E.values() for e in v}
    # QWI tables: county 4-digit (constraints) and state 6-digit x firm size (tilts), for stocks, hires, separations
    cty = {ms: {} for ms in MEASURES}; st6 = {ms: {} for ms in MEASURES}; sup = collections.Counter()
    for name in ('sa', 'se', 'rh'):
        d, s_ = load_qwi_demo(os.path.join(a.qwi, f'qwi_md_{name}_f_gc_n4_op_u_2021-2024.csv.gz'), lambda g, i: (g, i) in keys4, 'C')
        for ms in MEASURES: cty[ms][name] = d[ms]
        sup.update({f'county_{k}': v for k, v in s_.items()})
        d, s_ = load_qwi_demo(os.path.join(a.qwi, f'qwi_md_{name}_fs_gs_n6_op_u_2021-2024.csv.gz'), lambda g, i: i in inds6, 'S')
        for ms in MEASURES: st6[ms][name] = d[ms]
    st4 = {ms: {name: collections.defaultdict(lambda: collections.defaultdict(float)) for name in st6[ms]} for ms in MEASURES}
    for ms in MEASURES:
        for name, d in st6[ms].items():
            for (g_, ind, y, q, fs), v in d.items():
                if fs != '0': continue
                for grp, x in v.items():
                    if grp[0] != 'supp': st4[ms][name][(ind[:4], y, q)][grp] += x
    lam = {k: 1.0 for k in SIZES}
    if a.tilt_lambda:
        with open(cond.require(a.tilt_lambda)) as fh: lam.update({r['firm_size_class']: float(r['lambda']) for r in csv.DictReader(fh)})
    # ACS joint priors (systematic)
    acs = {}
    if a.prior == 'systematic':
        import pyarrow.parquet as pq
        t = pq.read_table(cond.require(a.acs), columns=['sample_type', 'sector', 'powpuma_counties', 'PERWT', 'SEX', 'agegrp', 'education', 'race', 'ethnicity']).to_pylist()
        cnt = collections.defaultdict(collections.Counter)
        for p in t:
            if p['sample_type'] != '1-year' or p['agegrp'] not in AGES: continue
            e = 'E5' if p['agegrp'] in YOUNG else p['education']
            if e not in EDU or (e == 'E5') != (p['agegrp'] in YOUNG): continue
            ty = (str(p['SEX']), p['agegrp'], e, re_group(p['race'], p['ethnicity']))
            cnt[(p['sector'], 'MD')][ty] += p['PERWT']
            for c in (p['powpuma_counties'] or '').replace(',', ' ').split():  # space-separated county list
                if c: cnt[(p['sector'], c.strip())][ty] += p['PERWT']
        acs = cnt
    icc = lodes_icc(a.lodes, {priors.bds_sector(n) for n in inds6}) if a.prior == 'systematic' else {}
    log = collections.Counter(); checks = collections.defaultdict(list); tilts = {}; prev_share = {}

    def cell_size_margins(ms, name, rows, c, ind4, y, q):
        """{((n, fs), group): value}: the county 4-digit composition of measure ms split across (cell, size) with state
        6-digit x firm-size tilts shrunk by lambda (D13); raked so the slice reproduces the county composition."""
        tot = sum(rows.values())
        ref = st4[ms][name].get((ind4, y, q)) or {}
        ref_sh = shares(ref, name) if ref else None
        csh = shares(cty[ms][name].get((c, ind4, y, q, '0'), {}), name, ref_sh)
        if csh is None: csh = ref_sh; log[f'county_{name}_{ms}_missing'] += 1
        if csh is None: csh = {g: 1 / len(table_groups(name)) for g in table_groups(name)}
        seed = {}
        for (n, fs), e in rows.items():
            s6 = st6[ms][name].get(('24000', n, y, q, fs)); s6sh = shares(s6, name, ref_sh) if s6 else None
            for g in table_groups(name):
                tilt = (s6sh[g] / ref_sh[g]) if s6sh and ref_sh and ref_sh.get(g, 0) > 0 and s6sh.get(g, 0) > 0 else 1.0
                seed[((n, fs), g)] = e * max(csh[g], 1e-6) * min(max(tilt, 0.1), 10.0) ** lam.get(fs, 1.0)
        M = priors.ipf_np(seed, dict(rows), {g: csh[g] * tot for g in table_groups(name)})
        checks[f'{name}_{ms}_county_gap'].append(sum(abs(sum(M.get((k, g), 0) for k in rows) / tot - csh[g]) for g in table_groups(name)) / 2)
        return M

    def joint(marg, key, e_tot, seed):
        sa = {(g[1], g[2]): marg['sa'].get((key, g), 0.0) for g in table_groups('sa')}
        se = {(g[1], g[2]): marg['se'].get((key, g), 0.0) for g in table_groups('se')}
        rh = {g[1]: marg['rh'].get((key, g), 0.0) for g in table_groups('rh')}
        for s_ in SEX:  # sex x education scaled to the sex totals of sex x age; education 'not available' = under 25
            ta, te = sum(v for (x, _), v in sa.items() if x == s_), sum(v for (x, _), v in se.items() if x == s_)
            for k in [k for k in se if k[0] == s_]: se[k] = se[k] * ta / te if te > 0 else 0.0
            young = sum(v for (x, a_), v in sa.items() if x == s_ and a_ in YOUNG); old = sum(v for (x, a_), v in sa.items() if x == s_ and a_ not in YOUNG)
            e5 = se.get((s_, 'E5'), 0.0); eo = sum(v for (x, e_), v in se.items() if x == s_ and e_ != 'E5')
            se[(s_, 'E5')] = young
            for k in [k for k in se if k[0] == s_ and k[1] != 'E5']: se[k] = se[k] * old / eo if eo > 0 else old / 4
            if abs(e5 - young) > 0.05 * max(young, 1): log['e5_vs_under25_adjusted'] += 1
        rt = sum(rh.values()); rh = {k: v * e_tot / rt for k, v in rh.items()} if rt > 0 else {k: e_tot / len(RE) for k in RE}
        return ipf_types(seed, [(lambda t: (t[0], t[1]), sa), (lambda t: (t[0], t[2]), se), (lambda t: t[3], rh)])

    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, f'workers_{a.tag}.csv.gz.part')
    held = {ms: collections.defaultdict(lambda: collections.defaultdict(float)) for ms in ('emp', 'HirA')}
    seg = collections.defaultdict(list)
    with gzip.open(tmp, 'wt', newline='') as fo:
        w = csv.writer(fo)
        w.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'est_id', 'firm_size_class', 'sex', 'agegrp', 'education', 're', 'emp', 'hires', 'separations'])
        for (m, y, q, c, ind4), ests in sorted(E.items()):
            rng = cond.rng_for(a.seed, m, y, q, c, ind4)
            exits = X_exits.get((m, y, q, c, ind4), [])
            rows = {'emp': collections.Counter(), 'HirA': collections.Counter(), 'Sep': collections.Counter()}
            for n, fs, _, e, h, sp in ests: rows['emp'][(n, fs)] += e; rows['HirA'][(n, fs)] += h; rows['Sep'][(n, fs)] += sp
            for n, fs, _, e in exits: rows['Sep'][(n, fs)] += e
            if sum(rows['emp'].values()) <= 0: continue
            marg = {ms: {name: cell_size_margins(ms, name, {k: v for k, v in rows[ms].items() if v > 0}, c, ind4, y, q) for name in ('sa', 'se', 'rh')}
                    for ms in MEASURES if sum(rows[ms].values()) > 0}
            keys = set(rows['emp']) | set(rows['Sep'])
            for (n, fs) in sorted(keys):
                e_tot = rows['emp'].get((n, fs), 0.0)
                out_rows = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
                members = [(est, e) for n_, fs_, est, e, _, _ in ests if (n_, fs_) == (n, fs) and e > 0]
                J = {}
                if e_tot > 0 and members:
                    # stocks: joint, then establishments
                    if a.prior == 'systematic':
                        pr = acs.get((priors.bds_sector(n), c)) or acs.get((priors.bds_sector(n), 'MD')) or {}
                        ps = sum(pr.values()) or 1.0
                        seed = {t: pr.get(t, 0.0) / ps + 1e-4 for t in TYPES}
                        log['acs_area_prior' if acs.get((priors.bds_sector(n), c)) else 'acs_state_prior'] += 1
                    else:
                        seed = {t: 1.0 for t in TYPES}
                    J = joint(marg['emp'], (n, fs), e_tot, seed)
                    if a.prior == 'systematic':
                        rho = icc.get(priors.bds_sector(n), {}); seedE = {}
                        for est, e in members:
                            key = (m, est)
                            if key not in tilts:
                                par = parents.get(key)
                                if par and (m, par) in tilts: tilts[key] = tilts[(m, par)]   # split successor inherits
                                else:
                                    tl = {}
                                    for d, (cols, f) in LODES_DIMS.items():
                                        k_ = len(cols); r_ = rho.get(d, 1e-4); kappa = (1 - r_) / r_
                                        tl[d] = [x * k_ for x in dirichlet(rng, [kappa / k_] * k_)]
                                    tilts[key] = tl
                            tl = tilts[key]
                            for t, v in J.items():
                                if v <= 0: continue
                                f_ = 1.0
                                for d, (cols, f) in LODES_DIMS.items():
                                    ix = f(t)
                                    if ix is not None: f_ *= tl[d][ix]
                                seedE[(est, t)] = e * v / e_tot * f_
                        X = priors.ipf_np(seedE, {est: e for est, e in members}, {t: v for t, v in J.items() if v > 0})
                    else:
                        X = {(est, t): e * v / e_tot for est, e in members for t, v in J.items() if v > 0}
                    for (est, t), v in X.items(): out_rows[(est, t)][0] = v
                    cur_share = collections.defaultdict(dict)
                    for (est, t), v in X.items(): cur_share[est][t] = v
                    for est, e in members: prev_share[(m, est)] = {t: v / e for t, v in cur_share[est].items()}
                    for t, v in J.items(): held['emp'][(m, c, priors.bds_sector(n), y, q, fs)][(t[0], t[1])] += v
                    if len(members) >= 5:
                        for lab, fsel in (('hispanic', lambda t: t[3] == 'H'), ('young', lambda t: t[1] in YOUNG)):
                            Ag = collections.Counter()
                            for (es, t), v in X.items():
                                if fsel(t): Ag[es] += v
                            Bg = {est: e - Ag[est] for est, e in members}
                            sa_, sb_ = sum(Ag.values()), sum(Bg.values())
                            if sa_ > 0 and sb_ > 0: seg[lab].append((e_tot, 0.5 * sum(abs(Ag[k] / sa_ - Bg[k] / sb_) for k in Ag)))
                # flows: hires and separations by type, drawn from each establishment's own composition
                Jsh = {t: v / e_tot for t, v in J.items()} if e_tot > 0 else None
                for ix, ms in ((1, 'HirA'), (2, 'Sep')):
                    f_tot = rows[ms].get((n, fs), 0.0)
                    if f_tot <= 0 or ms not in marg: continue
                    fmem = [(est, x) for n_, fs_, est, _, h, sp in ests if (n_, fs_) == (n, fs) for x in [(h if ms == 'HirA' else sp)] if x > 0]
                    if ms == 'Sep': fmem += [(est, e) for n_, fs_, est, e in exits if (n_, fs_) == (n, fs)]
                    if not fmem: continue
                    fseed0 = {t: (Jsh.get(t, 0.0) + 1e-4) for t in TYPES} if (Jsh and a.prior == 'systematic') else {t: 1.0 for t in TYPES}
                    Jf = joint(marg[ms], (n, fs), f_tot, fseed0)
                    seedF = {}
                    for est, x in fmem:
                        sh = prev_share.get((m, est)) if ms == 'Sep' and not any(est == e_ for e_, _ in members) else None
                        sh = sh or {t: out_rows[(est, t)][0] for t in TYPES if out_rows[(est, t)][0] > 0}
                        tot_sh = sum(sh.values())
                        if tot_sh <= 0: sh = Jsh or {t: 1.0 for t in TYPES}; tot_sh = sum(sh.values())
                        for t, v in sh.items():
                            if Jf.get(t, 0) > 0: seedF[(est, t)] = x * v / tot_sh
                    F = priors.ipf_np(seedF, {est: x for est, x in fmem}, {t: v for t, v in Jf.items() if v > 0})
                    for (est, t), v in F.items(): out_rows[(est, t)][ix] = v
                    if ms == 'HirA':
                        for t, v in Jf.items(): held['HirA'][(m, c, priors.bds_sector(n), y, q, fs)][(t[0], t[1])] += v
                    checks[f'{ms}_cell_total_gap'].append(abs(sum(F.values()) - f_tot) / f_tot)
                for (est, t), (e, h, sp) in out_rows.items():
                    if max(e, h, sp) >= 0.005: w.writerow([m, y, q, c, n, est, fs, *t, round(e, 3), round(h, 3), round(sp, 3)])
                log['cell_size_groups'] += 1
    os.replace(tmp, os.path.join(a.out, f'workers_{a.tag}.csv.gz'))
    report(a, held, checks, seg, log, icc, sup)


def report(a, held, checks, seg, log, icc, sup):
    L = [f'Step 4 worker groups ({a.prior}), versions {cond.versions_label(a.versions_seen)}, {a.tag}; {len(TYPES)} worker types; D13 tilt shrinkage: {a.tilt_lambda or "none"}',
         f'log {dict(log)}', f'QWI county 4-digit group cells suppressed: {dict(sup)}',
         'constraint checks (max over cell-quarters): slice composition vs QWI county 4-digit composition (TVD): ' +
         ', '.join(f'{k.replace("_county_gap", "")} {max(v):.1e}' for k, v in checks.items() if k.endswith('county_gap')),
         '  flows by type sum to the panel\'s cell hires / separations (relative gap): ' +
         ', '.join(f'{k.replace("_cell_total_gap", "")} {max(v):.1e}' for k, v in checks.items() if k.endswith('cell_total_gap'))]
    if icc: L.append('LODES within-county ICC for establishment tilts (systematic): ' + '; '.join(f'{s}: ' + ', '.join(f'{d} {r:.3f}' for d, r in v.items()) for s, v in icc.items()))
    obs = {'emp': collections.defaultdict(dict), 'HirA': collections.defaultdict(dict)}
    with cond.open_text(os.path.join(a.qwi, 'qwi_md_sa_fs_gc_ns_op_u_2021-2024.csv.gz')) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != 'C' or r['ownercode'] != 'A05' or r['firmsize'] == '0' or r['sex'] not in SEX or r['agegrp'] not in AGES: continue
            k = (r['geography'], r['industry'], int(r['year']), int(r['quarter']), r['firmsize'])
            v0, v1, h = cond.qwi_value(r, 'Emp'), cond.qwi_value(r, 'EmpEnd'), cond.qwi_value(r, 'HirA')
            if v0 is not None and v1 is not None: obs['emp'][k][(r['sex'], r['agegrp'])] = (v0 + v1) / 2
            if h is not None: obs['HirA'][k][(r['sex'], r['agegrp'])] = h
    L.append('\nHeld-out: sex x age shares within each firm-size class, panel vs QWI county x sector x firm size (TVD, mean over quarters and versions);')
    L.append('  baseline = the same composition for every size class (no firm-size structure). Worcester 72 is 95% covered by the slice, Montgomery 54 only 20%.')
    for ms, lab in (('emp', 'employment'), ('HirA', 'hires')):
        gaps = collections.defaultdict(list); base = collections.defaultdict(list)
        allsz = collections.defaultdict(lambda: collections.defaultdict(float))
        for (m, c, sec, y, q, fs), d in held[ms].items():
            for g, v in d.items(): allsz[(m, c, sec, y, q)][g] += v
        for (m, c, sec, y, q, fs), d in held[ms].items():
            o = obs[ms].get((c, sec, y, q, fs))
            if not o or len(o) < 12: continue
            to = sum(o.values()); tp = sum(d.values()); ta = sum(allsz[(m, c, sec, y, q)].values())
            if min(to, tp, ta) <= 0: continue
            gaps[(c, sec, fs)].append(sum(abs(d.get(g, 0) / tp - o[g] / to) for g in o) / 2)
            base[(c, sec, fs)].append(sum(abs(allsz[(m, c, sec, y, q)].get(g, 0) / ta - o[g] / to) for g in o) / 2)
        L.append(f'  {lab}:')
        for k in sorted(gaps):
            L.append(f'    {k[0]} sector {k[1]} firm size {k[2]}: panel {st.mean(gaps[k]):.3f}   no-size baseline {st.mean(base[k]):.3f}   (n={len(gaps[k])})')
    if seg:
        L.append('\nSegregation across establishments within (cell, size), dissimilarity index, employment-weighted mean:')
        for lab, v in seg.items():
            L.append(f'  {lab}: {sum(w * d for w, d in v) / sum(w for w, _ in v):.3f} (cell-size groups with >= 5 establishments: {len(v)})')
    print('\n'.join(L))
    open(os.path.join(a.out, f'worker_report_{a.tag}.txt'), 'w').write('\n'.join(L) + '\n')


if __name__ == '__main__':
    main()
