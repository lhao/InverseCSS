"""Structured priors for the systematic version (step 3d; SYNTHETIC_PANEL_DESIGN section 4).

The random baseline assumes no structure where the published tables are silent. The systematic version replaces that
with external evidence, and is raked or scaled to the SAME published totals:
  joint(...)        firm age x firm size employment shares by sector (BDS national sector x firm age x firm size),
                    used as the starting table for the joint draw of an establishment's firm age and size class
  dynamics(...)     relative establishment exit rates and continuers' job creation / destruction rates by firm age
                    class and by firm size class (BDS Maryland x sector, pooled 2021-2023). Class 0-1 uses age-1
                    firms only: age-0 firms are newborn, with no exits or continuers by definition
  churn(...)        relative churn rates (hires - job gains) by firm age class and firm size class
                    (QWI Maryland, state x 6-digit cells pooled to the sector, 2021-2024)
Relative rates are 1.0 for the sector average; age and size effects are combined multiplicatively (no interaction:
BDS publishes the age x size joint for Maryland only through the national table).

Class mapping to QWI (firmage 1: 0-1, 2: 2-3, 3: 4-5, 4: 6-10, 5: 11+; firmsize 1: 0-19, 2: 20-49, 3: 50-249, 4: 250-499,
5: 500+). BDS size classes 20-99 and 100-499 straddle QWI's bounds; their counts are split log-uniformly
(20-49 gets ln(50/20) / ln(100/20) = 0.57 of 20-99; 100-249 gets ln(250/100) / ln(500/100) = 0.57 of 100-499).
"""
import collections, csv, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond

AGES = ['1', '2', '3', '4', '5']
SIZES = ['1', '2', '3', '4', '5']
BDS_AGE = {'a) 0': '1', 'b) 1': '1', 'c) 2': '2', 'd) 3': '2', 'e) 4': '3', 'f) 5': '3', 'g) 6 to 10': '4',
           'h) 11 to 15': '5', 'i) 16 to 20': '5', 'j) 21 to 25': '5', 'k) 26+': '5', 'l) Left Censored': '5'}
_S1 = math.log(50 / 20) / math.log(100 / 20); _S2 = math.log(250 / 100) / math.log(500 / 100)
BDS_SIZE = {'a) 1 to 4': {'1': 1}, 'b) 5 to 9': {'1': 1}, 'c) 10 to 19': {'1': 1}, 'd) 20 to 99': {'2': _S1, '3': 1 - _S1},
            'e) 100 to 499': {'3': _S2, '4': 1 - _S2}, 'f) 500 to 999': {'5': 1}, 'g) 1000 to 2499': {'5': 1},
            'h) 2500 to 4999': {'5': 1}, 'i) 5000 to 9999': {'5': 1}, 'j) 10000+': {'5': 1}}
SECTOR = {'31': '31-33', '32': '31-33', '33': '31-33', '44': '44-45', '45': '44-45', '48': '48-49', '49': '48-49'}


def bds_sector(naics): return SECTOR.get(naics[:2], naics[:2])


def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def joint(path):
    """{(sector, year): {(age, size): employment share}} from BDS national sector x firm age x firm size.
    Suppressed cells (D) count as zero; a small floor keeps every combination possible."""
    acc = collections.defaultdict(collections.Counter)
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            e = num(r['emp'])
            if e is None or r['fage'] not in BDS_AGE or r['fsize'] not in BDS_SIZE: continue
            for s, f in BDS_SIZE[r['fsize']].items(): acc[(r['sector'], int(r['year']))][(BDS_AGE[r['fage']], s)] += e * f
    out = {}
    for k, c in acc.items():
        tot = sum(c.values())
        if tot <= 0: continue
        out[k] = {(a, s): max(c[(a, s)] / tot, 1e-6) for a in AGES for s in SIZES}
    return out


def dynamics(st_fa_path, st_fz_path, years=(2021, 2022, 2023)):
    """{sector: {'exit_age': {class: rel}, 'exit_size': ..., 'jc_age', 'jc_size', 'jd_age', 'jd_size'}} relative to the
    sector average, Maryland, pooled over `years`. jc / jd = continuers' job creation / destruction per job."""
    out = collections.defaultdict(dict)
    for path, col, key in ((st_fa_path, 'fage', 'age'), (st_fz_path, 'fsize', 'size')):
        acc = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
        with cond.open_text(cond.require(path)) as fh:
            for r in csv.DictReader(fh):
                if int(r['year']) not in years: continue
                if key == 'age' and r[col] == 'a) 0': continue   # newborn firms: no exits or continuers by definition
                cls = {BDS_AGE[r[col]]: 1} if key == 'age' else BDS_SIZE.get(r[col])
                if not cls: continue
                v = {k: num(r[k]) for k in ('estabs', 'estabs_exit', 'emp', 'job_creation_continuers', 'job_destruction_continuers')}
                for c, f in cls.items():
                    for k, x in v.items():
                        if x is not None: acc[r['sector']][c][k] += x * f
        for sec, d in acc.items():
            T = collections.Counter()
            for c in d.values(): T.update(c)
            def rel(c, num_, den):
                base = T[num_] / T[den] if T[den] else 0
                val = d[c][num_] / d[c][den] if d[c][den] else 0
                return val / base if base > 0 and val > 0 else 1.0
            classes = AGES if key == 'age' else SIZES
            out[sec][f'exit_{key}'] = {c: rel(c, 'estabs_exit', 'estabs') for c in classes}
            out[sec][f'jc_{key}'] = {c: rel(c, 'job_creation_continuers', 'emp') for c in classes}
            out[sec][f'jd_{key}'] = {c: rel(c, 'job_destruction_continuers', 'emp') for c in classes}
            if key == 'age':  # coarse groups (Hao, 2026-10-18): young 0-5 years, mature 6+
                g = {'young': ('1', '2', '3'), 'mature': ('4', '5')}
                tot_ex = T['estabs_exit'] / T['estabs'] if T['estabs'] else 0
                out[sec]['exit_agegrp'] = {k: (sum(d[c]['estabs_exit'] for c in cs) / sum(d[c]['estabs'] for c in cs) / tot_ex)
                                           if tot_ex > 0 and sum(d[c]['estabs'] for c in cs) > 0 else 1.0 for k, cs in g.items()}
    return dict(out)


def churn(fa_path, fs_path):
    """{sector: {'age': {class: rel}, 'size': {class: rel}}}: churn per job by firm age / size class relative to the
    sector average, from QWI state x 6-digit (published cells), pooled over all quarters."""
    out = collections.defaultdict(dict)
    for path, dim, key in ((fa_path, 'firmage', 'age'), (fs_path, 'firmsize', 'size')):
        acc = collections.defaultdict(collections.Counter)
        with cond.open_text(cond.require(path)) as fh:
            for r in csv.DictReader(fh):
                if r['geo_level'] != 'S' or r['ind_level'] != '6' or r['ownercode'] != 'A05' or r['sex'] != '0' or r['agegrp'] != 'A00' or r[dim] == '0':
                    continue
                v = {k: cond.qwi_value(r, k) for k in ('Emp', 'EmpEnd', 'HirA', 'FrmJbGn')}
                if None in v.values(): continue
                sec = bds_sector(r['industry'])
                acc[(sec, r[dim])]['churn'] += max(v['HirA'] - v['FrmJbGn'], 0); acc[(sec, r[dim])]['base'] += (v['Emp'] + v['EmpEnd']) / 2
        secs = {s for s, _ in acc}
        for sec in secs:
            ch = sum(acc[(sec, c)]['churn'] for c in AGES); b = sum(acc[(sec, c)]['base'] for c in AGES)
            avg = ch / b if b else 0
            out[sec][key] = {c: (acc[(sec, c)]['churn'] / acc[(sec, c)]['base'] / avg) if acc[(sec, c)]['base'] > 0 and avg > 0 and acc[(sec, c)]['churn'] > 0 else 1.0 for c in AGES}
    return dict(out)


AGE_GROUPS = {'young': ('1', '2', '3'), 'mature': ('4', '5')}


def age_group(age_class): return 'young' if age_class in AGE_GROUPS['young'] else 'mature'


def class_flow_rates(path, dim, coarse_age=True):
    """Job gain, job loss and churn rates by firm class from QWI state x 6-digit COUNTS (summed first, then divided;
    no published rate or turnover index is used). dim 'firmsize' -> classes 1-5; 'firmage' -> groups young / mature
    (coarse_age=True, used for the panel's constraints) or classes 1-5 (coarse_age=False, used for diagnostics).
    Returns lookup(naics6, y, q) -> ({class: (gain rate, loss rate, churn rate)}, level): the 6-digit industry if every
    class has data that quarter, else its 4-digit aggregate, else the sector (published 6-digit cells summed)."""
    acc = collections.defaultdict(collections.Counter)
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != 'S' or r['ind_level'] != '6' or r['ownercode'] != 'A05' or r['sex'] != '0' or r['agegrp'] != 'A00' or r[dim] == '0':
                continue
            v = {k: cond.qwi_value(r, k) for k in ('Emp', 'EmpEnd', 'FrmJbGn', 'FrmJbLs', 'HirA')}
            if None in v.values(): continue
            cls = r[dim] if dim == 'firmsize' or not coarse_age else age_group(r[dim])
            yq = (int(r['year']), int(r['quarter'])); ind = r['industry']
            for lvl, code in (('6', ind), ('4', ind[:4]), ('2', bds_sector(ind))):
                c = acc[(lvl, code) + yq + (cls,)]
                c['base'] += (v['Emp'] + v['EmpEnd']) / 2; c['gain'] += v['FrmJbGn']; c['loss'] += v['FrmJbLs']
                c['churn'] += max(v['HirA'] - v['FrmJbGn'], 0)
    classes = SIZES if dim == 'firmsize' or not coarse_age else list(AGE_GROUPS)

    def lookup(naics6, y, q):
        for lvl, code in (('6', naics6), ('4', naics6[:4]), ('2', bds_sector(naics6))):
            cs = [acc.get((lvl, code, y, q, k)) for k in classes]
            if all(c and c['base'] > 0 for c in cs):
                return {k: (c['gain'] / c['base'], c['loss'] / c['base'], c['churn'] / c['base']) for k, c in zip(classes, cs)}, lvl
        return None, 'none'
    return lookup


def ipf(seed, row_t, col_t, iters=60):
    """Raking of a {(row, col): value} table to row and column totals (both summing to the same total)."""
    m = {k: v for k, v in seed.items() if v > 0}
    for _ in range(iters):
        for t, idx in ((row_t, 0), (col_t, 1)):
            cur = collections.Counter()
            for k, v in m.items(): cur[k[idx]] += v
            for k in m:
                if cur[k[idx]] > 0: m[k] *= t.get(k[idx], 0.0) / cur[k[idx]]
    return m



def ipf_np(seed, row_t, col_t, iters=60):
    """Same raking as ipf() (same iterations and rules), with numpy arrays: about 20-50x faster on large tables
    (establishments x worker types in step 4). Results equal ipf() up to floating-point summation order (~1e-12).
    Steps 3a / 3b keep ipf() so their outputs stay byte-identical to earlier runs."""
    import numpy as np
    keys = [k for k, v in seed.items() if v > 0]
    if not keys: return {}
    rows = sorted({k[0] for k in keys}, key=str); cols = sorted({k[1] for k in keys}, key=str)
    ri = {r: i for i, r in enumerate(rows)}; ci = {c: j for j, c in enumerate(cols)}
    M = np.zeros((len(rows), len(cols)))
    I = np.fromiter((ri[k[0]] for k in keys), int, len(keys)); J = np.fromiter((ci[k[1]] for k in keys), int, len(keys))
    M[I, J] = [seed[k] for k in keys]
    rt = np.array([row_t.get(r, 0.0) for r in rows], float); ct = np.array([col_t.get(c, 0.0) for c in cols], float)
    for _ in range(iters):
        cur = M.sum(axis=1); f = np.where(cur > 0, rt / np.where(cur > 0, cur, 1.0), 1.0); M *= f[:, None]
        cur = M.sum(axis=0); f = np.where(cur > 0, ct / np.where(cur > 0, cur, 1.0), 1.0); M *= f[None, :]
    return {k: float(M[i, j]) for k, i, j in zip(keys, I, J)}

# ---------------------------------------------------------------- D11: county firm-size targets
BANDS = ((50, '<50'), (250, '50-249'), (1000, '250-999'), (5000, '1000-4999'), (float('inf'), '5000+'))


def band(x): return next(b for hi, b in BANDS if x < hi)


def county_sector_size(path):
    """{(county, sector, y, q): {class: employment}} from QWI county x sector by firm size, published classes only."""
    out = collections.defaultdict(dict)
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != 'C' or r['ownercode'] != 'A05' or r['sex'] != '0' or r['agegrp'] != 'A00' or r['firmsize'] == '0': continue
            e0, e1 = cond.qwi_value(r, 'Emp'), cond.qwi_value(r, 'EmpEnd')
            if e0 is None or e1 is None: continue
            out[(r['geography'], r['industry'], int(r['year']), int(r['quarter']))][r['firmsize']] = (e0 + e1) / 2
    return out


def implied(cells, fsz, y, q):
    """Implied firm-size class employment for a set of {naics6: employment}; also the covered employment share."""
    I = collections.Counter(); cov = tot = 0.0
    for n, e in cells.items():
        tot += e
        m = fsz.get((n, y, q))
        if not m: continue
        cov += e
        for k, s in m.items(): I[k] += e * s
    return I, (cov / tot if tot else 0.0)


def blend(I, O, w_of):
    """Blended class shares: on published classes, implied^(1-w) x observed^w (renormalised to the implied share of
    those classes); unpublished classes keep the implied share. w_of(k, O_k) gives the weight."""
    ti = sum(I.values())
    if ti <= 0: return None
    ish = {k: I.get(k, 0.0) / ti for k in SIZES}
    P = [k for k in SIZES if k in O and O[k] > 0 and ish[k] > 0]
    if not P: return ish
    to = sum(O[k] for k in P); tip = sum(ish[k] for k in P)
    b = {k: math.exp((1 - w_of(k, O[k])) * math.log(ish[k] / tip) + w_of(k, O[k]) * math.log(O[k] / to)) for k in P}
    sb = sum(b.values())
    out = dict(ish)
    for k in P: out[k] = tip * b[k] / sb
    return out



def load_transfer_var(path):
    """{size band: transfer variance} from calibrate_county_size.py."""
    with open(cond.require(path)) as fh:
        return {r['band']: float(r['transfer_var']) for r in csv.DictReader(fh)}


def county_weight(tv):
    """w(class, observed class employment) = transfer var / (transfer var + noise var), noise from SIGMA_STOCK."""
    def w(k, ok):
        t = tv.get(band(ok), 0.0); nv = cond.sigma(cond.SIGMA_STOCK, ok) ** 2
        return t / (t + nv) if t + nv > 0 else 0.0
    return w


def county_size_targets(qcew_sector, fsz, obs, tv, floor=1e-4):
    """{(version, county, naics6, y, q): {size class: share}}: firm-size targets for every 6-digit cell, consistent with
    the BLENDED county x sector shares (state-implied x QWI county, weighted by reliability; D11) and keeping each
    industry's own profile from the state 6-digit margins (IPF: rows = industries' employment, columns = blended
    county-sector class employment; seed = employment x state shares, floored). qcew_sector = load_qcew(...) output
    restricted to the whole sectors of interest."""
    wfun = county_weight(tv); out = {}
    for (y, q), per in qcew_sector.items():
        for m, cells in per.items():
            by_cs = collections.defaultdict(dict)
            for (c, n), (est, m1, m2, m3, wg) in cells.items():
                e = (m1 + m2 + m3) / 3
                if e > 0: by_cs[(c, bds_sector(n))][n] = e
            for (c, sec), cl in by_cs.items():
                I, cov = implied(cl, fsz, y, q)
                if sum(I.values()) <= 0: continue
                O = obs.get((c, sec, y, q), {})
                B = blend(I, O, wfun) if O else {k: I.get(k, 0.0) / sum(I.values()) for k in SIZES}
                tot = sum(cl.values()); ish = {k: I.get(k, 0.0) / sum(I.values()) for k in SIZES}
                seed = {}
                for n, e in cl.items():
                    s6 = fsz.get((n, y, q)) or ish
                    for k in SIZES: seed[(n, k)] = e * max(s6.get(k, 0.0), floor)
                M = ipf(seed, dict(cl), {k: B[k] * tot for k in SIZES})
                for n, e in cl.items():
                    row = {k: M.get((n, k), 0.0) for k in SIZES}; sr = sum(row.values())
                    if sr > 0: out[(m, c, n, y, q)] = {k: v / sr for k, v in row.items()}
    return out


def rake_joint(sizes, feas_size, tgt_age, tgt_size, prior, u, iters=80):
    """Joint draw of (age class, size class) per establishment. Probabilities proportional to prior(a, s) x w_a x v_s
    over feasible sizes; w and v raked so expected employment shares equal both target margins."""
    w = {a: 1.0 for a in AGES}; v = {s: 1.0 for s in SIZES}; tot = sum(sizes) or 1.0
    for _ in range(iters):
        ea = collections.Counter(); es = collections.Counter()
        for x, F in zip(sizes, feas_size):
            z = sum(prior[(a, s)] * w[a] * v[s] for a in AGES for s in F)
            for a in AGES:
                for s in F:
                    p = x * prior[(a, s)] * w[a] * v[s] / z; ea[a] += p; es[s] += p
        for a in AGES:
            if ea[a] > 0 and tgt_age.get(a, 0) > 0: w[a] *= (tgt_age[a] * tot / ea[a]) ** 0.5
        for s in SIZES:
            if es[s] > 0 and tgt_size.get(s, 0) > 0: v[s] *= (tgt_size[s] * tot / es[s]) ** 0.5
    out = []
    for F, ui in zip(feas_size, u):
        pairs = [(a, s) for a in AGES for s in F]
        z = sum(prior[p] * w[p[0]] * v[p[1]] for p in pairs); acc = 0.0; pick = pairs[-1]
        for p in pairs:
            acc += prior[p] * w[p[0]] * v[p[1]] / z
            if ui <= acc: pick = p; break
        out.append(pick)
    return out


if __name__ == '__main__':
    root = sys.argv[1] if len(sys.argv) > 1 else '.'
    bds = os.path.join(root, 'data/raw/external/bds'); qwi = os.path.join(root, 'data/raw/qwi/R2026Q3')
    J = joint(os.path.join(bds, 'bds2023_sec_fa_fz_2019-2023.csv.gz'))
    D = dynamics(os.path.join(bds, 'bds2023_st_sec_fa_md_2019-2023.csv.gz'), os.path.join(bds, 'bds2023_st_sec_fz_md_2019-2023.csv.gz'))
    C = churn(os.path.join(qwi, 'qwi_md_sa_fa_gs_n6_op_u_2021-2024.csv.gz'), os.path.join(qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'))
    for sec in ('54', '72'):
        j = J[(sec, 2023)]
        print(f'sector {sec}: BDS national 2023 employment share by firm age (rows 0-1, 2-3, 4-5, 6-10, 11+) x size (0-19 ... 500+)')
        for a in AGES: print('   ', ' '.join(f'{j[(a, s)]:.3f}' for s in SIZES))
        for k in ('exit_age', 'exit_size', 'jc_age', 'jd_age', 'jc_size', 'jd_size'):
            print(f'  {k:9s}', ' '.join(f'{D[sec][k][c]:.2f}' for c in AGES))
        print('  churn age', ' '.join(f'{C[sec]["age"][c]:.2f}' for c in AGES), '| size', ' '.join(f'{C[sec]["size"][c]:.2f}' for c in SIZES))
